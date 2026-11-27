"""S2 labeling: verification of LLM output, shortlist routing, budget guard, real prompt rendering."""
import json
from datetime import timedelta

import pytest

from jobpipe.adapters.notifier.telegram import RecordingNotifier
from jobpipe.adapters.storage.db import open_db
from jobpipe.clock import iso
from jobpipe.config.settings import LLMConfig, LLMProvider, LLMTask, Secrets
from jobpipe.ports.llm import LLMResponse
from jobpipe.services import control, label
from jobpipe.services.events import create_entity, transition
from jobpipe.services.llm_router import LLMUnavailable, Router
from jobpipe.services.prompts import PromptStore

from .conftest import REPO


def mk_label(jid, level=25, stack=25, legal=22, comp=2, action="apply_now", **kw):
    return label.JobLabel(id=jid, breakdown=label.Breakdown(level=level, stack=stack, legal=legal, comp=comp),
                          action=action, **kw)


def test_fresh_points(clock):
    now = clock.now()
    assert label.fresh_points(iso(now - timedelta(days=1)), now) == 10
    assert label.fresh_points(iso(now - timedelta(days=6)), now) == 7
    assert label.fresh_points(iso(now - timedelta(days=60)), now) == 0
    assert label.fresh_points(None, now) == 2


def test_verify_clamps_recomputes_and_rejects_unquoted_evidence(clock):
    lab = mk_label("x", level=40, stack=-3, legal=25, comp=99, visa="explicit",
                   visa_evidence="we sponsor all visas", remote_scope="worldwide", remote_evidence="Remote OK")
    text = "C++ engineer. Remote OK for anyone. Great office."
    out = label.verify(lab, text, None, clock.now())
    # legal: the sponsorship claim was rejected, the quoted "worldwide" remote is worth at most 22
    assert out["breakdown"] == {"level": 25, "stack": 0, "legal": 22, "comp": 15, "fresh": 2}
    assert out["score"] == 64
    assert out["visa"] == "unknown" and out["visa_evidence"] is None and "visa" in out["unverified"]
    assert out["remote_scope"] == "worldwide" and out["remote_evidence"] == "Remote OK"


@pytest.mark.parametrize("visa,remote", [("relocation_only", "emea"), ("no", "listed")])
def test_every_unquoted_visa_or_remote_claim_becomes_unknown(clock, visa, remote):
    """Review of S2: weaker claims (relocation_only, emea, listed) need a real quote too."""
    lab = mk_label("x", visa=visa, visa_evidence="relocation package offered",
                   remote_scope=remote, remote_evidence="remote within EMEA")
    out = label.verify(lab, "C++ engineer in Amsterdam.", None, clock.now())
    assert out["visa"] == "unknown" and out["remote_scope"] == "unknown"
    assert set(out["unverified"]) == {"visa", "remote_scope"}


def test_office_only_and_unknown_are_left_alone(clock):
    out = label.verify(mk_label("x", remote_scope="n/a"), "C++ engineer, office.", None, clock.now())
    assert out["remote_scope"] == "n/a" and "unverified" not in out


def test_evidence_matching_is_whitespace_and_case_tolerant():
    assert label.evidence_ok("Visa   SPONSORSHIP available", "We have visa sponsorship available. Apply")
    assert not label.evidence_ok("visa sponsorship available", "We have no sponsorship")
    assert not label.evidence_ok(None, "text")


class FakeRouter:
    """Returns labels for whatever ids the batch contains, via a per-title rule."""

    def __init__(self, rule=None, fail=False):
        self.rule = rule or (lambda item: mk_label(item["id"]))
        self.fail = fail
        self.calls = []

    def complete(self, task, variables, schema=None, personal=False):
        assert task == "label" and not personal
        items = json.loads(variables["jobs"])
        self.calls.append(items)
        if self.fail:
            raise LLMUnavailable("label", ["x: 503"])
        labels = [self.rule(it) for it in items]
        return type("C", (), {"data": label.LabelBatch(labels=[x for x in labels if x])})()


def seed(conn, clock, n=3, protected_ids=(), **data):
    for i in range(n):
        jid = f"t:{i}"
        create_entity(conn, clock, "job", jid, columns={
            "source": "t", "kind": "ats", "company": f"Co{i}", "title": f"C++ Engineer {i}", "url": f"https://x/{i}",
            "posted_at": iso(clock.now()), "protected": 1 if jid in protected_ids else 0,
        }, data={"description": "Low latency C++. Visa sponsorship available.", "excerpt": "Low latency C++.", **data})
        transition(conn, clock, "job", jid, "prefiltered")


@pytest.fixture
def env(app_cfg, clock):
    conn = open_db(app_cfg.paths.db_path, clock)
    return conn, app_cfg, RecordingNotifier()


def states(conn):
    return dict(conn.execute("SELECT id, state FROM jobs").fetchall())


def test_run_labels_and_routes(env, clock):
    conn, cfg, notifier = env
    seed(conn, clock, n=3, protected_ids=("t:2",))

    def rule(item):
        if item["title"].endswith("1"):
            return mk_label(item["id"], level=3, stack=3, legal=3, comp=2, action="skip")
        return mk_label(item["id"])

    res = label.run(conn, clock, cfg, FakeRouter(rule), notifier)
    assert (res.labeled, res.shortlisted, res.skipped, res.protected) == (3, 2, 1, 1)
    assert states(conn) == {"t:0": "shortlisted", "t:1": "skipped", "t:2": "shortlisted"}
    stored = json.loads(conn.execute("SELECT data_json FROM jobs WHERE id='t:0'").fetchone()[0])["label"]
    assert stored["score"] == 25 + 25 + 8 + 2 + 10      # no visa/remote claim: legal capped at "unknown"


def test_protected_is_shortlisted_even_with_low_score(env, clock):
    conn, cfg, notifier = env
    seed(conn, clock, n=1, protected_ids=("t:0",))
    label.run(conn, clock, cfg, FakeRouter(lambda it: mk_label(it["id"], 0, 0, 0, 0, action="skip")), notifier)
    assert states(conn)["t:0"] == "shortlisted"


def test_missing_or_foreign_ids_leave_jobs_prefiltered(env, clock):
    conn, cfg, notifier = env
    seed(conn, clock, n=2)
    rule = lambda it: mk_label("j99") if it["id"] == "j1" else mk_label(it["id"])  # noqa: E731
    res = label.run(conn, clock, cfg, FakeRouter(rule), notifier)
    assert res.labeled == 1 and "prefiltered" in states(conn).values()


def test_failed_batch_counts_and_keeps_jobs(env, clock):
    conn, cfg, notifier = env
    seed(conn, clock, n=2)
    res = label.run(conn, clock, cfg, FakeRouter(fail=True), notifier)
    assert res.failed_batches == 1 and set(states(conn).values()) == {"prefiltered"}


def test_batches_and_limit(env, clock):
    conn, cfg, notifier = env
    cfg.settings.label.batch_size = 2
    seed(conn, clock, n=5)
    router = FakeRouter()
    res = label.run(conn, clock, cfg, router, notifier, limit=4)
    assert res.labeled == 4 and [len(c) for c in router.calls] == [2, 2]
    assert [it["id"] for it in router.calls[0]] == ["j1", "j2"]


def test_budget_guard_stops_and_notifies_once(env, clock):
    conn, cfg, notifier = env
    cfg.settings.label.daily_max_jobs = 2
    seed(conn, clock, n=4)
    assert label.run(conn, clock, cfg, FakeRouter(), notifier).labeled == 2
    r2 = label.run(conn, clock, cfg, FakeRouter(), notifier)
    r3 = label.run(conn, clock, cfg, FakeRouter(), notifier)
    assert r2.budget_stop and r3.budget_stop
    assert sum("лимит" in m for m in notifier.messages) == 1


def test_paused_does_nothing(env, clock):
    conn, cfg, notifier = env
    seed(conn, clock, n=1)
    control.set_flag(conn, clock, control.PAUSED, "1")
    assert label.run(conn, clock, cfg, FakeRouter(), notifier).labeled == 0


def test_real_router_and_prompt_round_trip(env, clock):
    """The repo prompt renders, the router validates LabelBatch, labels map back to job ids."""
    conn, cfg, notifier = env
    seed(conn, clock, n=2)

    class T:
        def __init__(self):
            self.req = None

        def complete(self, provider, key, request):
            self.req = request
            body = {"labels": [
                {"id": "j1", "breakdown": {"level": 25, "stack": 25, "legal": 22, "comp": 2}, "track": "HFT",
                 "level": "mid", "visa": "explicit", "visa_evidence": "Visa sponsorship available",
                 "remote_scope": "n/a", "resume_variant": "hft", "action": "apply_now", "reason": "fits"},
                {"id": "j2", "breakdown": {"level": 8, "stack": 3, "legal": 8, "comp": 2}, "track": "other",
                 "action": "skip", "reason": "no"},
            ]}
            return LLMResponse(json.dumps(body), 500, 200)

    llm = LLMConfig(providers={"g": LLMProvider(base_url="https://g", key_env="K", model="m")},
                    tasks={"label": LLMTask(chain=["g"], prompt_version="v1", max_tokens=8000)})
    t = T()
    router = Router(conn, clock, llm, Secrets({"K": "k" * 10}), t, PromptStore(REPO / "prompts"))
    res = label.run(conn, clock, cfg, router, notifier)
    assert res.labeled == 2 and res.shortlisted == 1
    prompt = t.req.messages[-1]["content"]
    assert '"id": "j1"' in prompt and "C++ Engineer" in prompt
    assert "KZ" in t.req.messages[0]["content"]
    lab = json.loads(conn.execute("SELECT data_json FROM jobs WHERE id='t:0'").fetchone()[0])["label"]
    assert lab["track"] == "hft" and lab["visa"] == "explicit" and lab["visa_evidence"]


def test_rejected_claims_earn_no_legal_points(clock):
    """S2 review: legal=25 for a sponsorship claim whose quote is not in the posting must not stay."""
    lab = mk_label("x", legal=25, visa="explicit", visa_evidence="we sponsor visas",
                   remote_scope="worldwide", remote_evidence="fully remote")
    out = label.verify(lab, "C++ engineer in our Amsterdam office.", None, clock.now())
    assert out["breakdown"]["legal"] == label.LEGAL_UNKNOWN
    assert out["score"] == sum(out["breakdown"].values())


def test_a_quoted_claim_keeps_its_legal_points(clock):
    lab = mk_label("x", legal=22, visa="explicit", visa_evidence="we sponsor visas",
                   remote_scope="worldwide", remote_evidence="fully remote")
    out = label.verify(lab, "C++ engineer. We sponsor visas.", None, clock.now())
    assert out["visa"] == "explicit" and out["breakdown"]["legal"] == 22


def test_partial_rejection_caps_legal_at_the_surviving_claim(clock):
    """S2 review: a fabricated sponsorship quote next to a quoted remote EMEA is worth EMEA's 12."""
    lab = mk_label("x", legal=25, visa="explicit", visa_evidence="we sponsor visas",
                   remote_scope="emea", remote_evidence="remote within EMEA")
    out = label.verify(lab, "C++ engineer, remote within EMEA.", None, clock.now())
    assert out["visa"] == "unknown" and out["remote_scope"] == "emea" and out["breakdown"]["legal"] == 12


def test_unknown_everything_with_high_legal_is_capped(clock):
    out = label.verify(mk_label("x", legal=25), "C++ engineer.", None, clock.now())
    assert out["breakdown"]["legal"] == label.LEGAL_UNKNOWN and "unverified" not in out


def test_job_the_model_keeps_skipping_is_dropped_after_three_runs(env, clock):
    conn, cfg, notifier = env
    seed(conn, clock, n=2)
    skip_second = FakeRouter(lambda it: None if it["id"] == "j2" or it["title"].endswith("1") else mk_label(it["id"]))
    for _ in range(2):
        label.run(conn, clock, cfg, skip_second, notifier)
    assert states(conn)["t:1"] == "prefiltered"                      # two misses: still retried
    label.run(conn, clock, cfg, skip_second, notifier)
    row = conn.execute("SELECT state, drop_rule FROM jobs WHERE id = 't:1'").fetchone()
    assert tuple(row) == ("dropped", "label_missing")


def test_rating_stops_after_the_30_sample(env, clock):
    from jobpipe.services import cards

    conn, cfg, notifier = env
    seed(conn, clock, n=31)
    label.run(conn, clock, cfg, FakeRouter(), notifier)
    for _ in range(cards.RATING_SAMPLE):
        _, buttons = cards.next_rating_card(conn)
        cards.record_rating(conn, clock, int(buttons[0][0][1].split(":")[2]), "up")
    assert cards.next_rating_card(conn) is None


def test_unexpected_enum_values_fall_back_to_safe_defaults():
    """Live run: one odd resume_variant rejected a whole batch of 20 labels."""
    lab = label.JobLabel.model_validate({
        "id": "j1", "breakdown": {"level": 20, "stack": 20, "legal": 8, "comp": 5},
        "resume_variant": "cpp_lowlatency", "track": "Quant Dev", "visa": "maybe?", "remote_scope": "EU only",
        "action": "Apply Now", "level": "Middle", "reason": "x" * 600})
    assert (lab.resume_variant, lab.track, lab.visa, lab.remote_scope) == ("generic", "other", "unknown", "unknown")
    assert lab.action == "apply_now" and lab.level == "unknown" and len(lab.reason) == 400
