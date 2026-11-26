import json
from dataclasses import replace

import pytest

from jobpipe.config.settings import PolicyCfg, RetentionCfg, RunnerCfg, Settings
from jobpipe.domain import states
from jobpipe.services import control
from jobpipe.services.events import create_entity, transition
from jobpipe.services.policy import ApplyFacts, Route, decide_apply, decide_message, needs_sponsorship
from jobpipe.services.queue import Queue
from jobpipe.services.retention import run_retention

# --- auto-apply policy (ADR 0016) -------------------------------------------------------

CLEAN = ApplyFacts(platform="hh", protected=False, dry_run=False, paused=False, already_applied=False,
                   truth_ok=True, unanswered_questions=0, similar_role_warning=False, applied_today=0)


def test_clean_application_is_auto_submitted():
    assert decide_apply(PolicyCfg(), CLEAN).route is Route.SUBMIT


@pytest.mark.parametrize("change,route,reason", [
    ({"already_applied": True}, Route.BLOCK, "already applied"),
    ({"truth_ok": False}, Route.BLOCK, "truth gate"),
    ({"paused": True}, Route.DEFER, "kill switch"),
    ({"applied_today": 20}, Route.DEFER, "daily cap"),
    ({"dry_run": True}, Route.DRY_RUN, "dry run"),
    ({"protected": True}, Route.ASK, "protected"),
    ({"unanswered_questions": 2}, Route.ASK, "without an answer"),
    ({"similar_role_warning": True}, Route.ASK, "similar role"),
])
def test_any_failed_check_leaves_auto_mode(change, route, reason):
    d = decide_apply(PolicyCfg(), replace(CLEAN, **change))
    assert d.route is route
    assert any(reason in r for r in d.reasons)


def test_block_beats_everything_and_ask_collects_all_reasons():
    d = decide_apply(PolicyCfg(), replace(CLEAN, already_applied=True, protected=True))
    assert d.route is Route.BLOCK
    d = decide_apply(PolicyCfg(), replace(CLEAN, protected=True, unanswered_questions=1))
    assert d.route is Route.ASK and len(d.reasons) == 2


def test_salary_question_asks_the_owner_by_default():
    d = decide_apply(PolicyCfg(), replace(CLEAN, topics_asked=frozenset({"compensation"}),
                                          topics_preapproved=frozenset({"compensation"}), posting_salary_known=True))
    assert d.route is Route.ASK and "compensation: always asks you" in d.reasons[0]


def test_salary_from_posting_range_when_switched_on():
    p = PolicyCfg(sensitive_answers={"compensation": "posting_range", "default": "preapproved"})
    asked = replace(CLEAN, topics_asked=frozenset({"compensation"}))
    assert decide_apply(p, replace(asked, posting_salary_known=True)).route is Route.SUBMIT
    d = decide_apply(p, asked)
    assert d.route is Route.ASK and "no salary range" in d.reasons[0]


@pytest.mark.parametrize("country,route", [("NL", Route.SUBMIT), ("KZ", Route.SUBMIT), ("am", Route.SUBMIT),
                                           (None, Route.ASK)])
def test_visa_question_is_derived_from_country(country, route):
    f = replace(CLEAN, topics_asked=frozenset({"visa", "github_url"}), job_country=country)
    assert decide_apply(PolicyCfg(), f).route is route


def test_needs_sponsorship():
    p = PolicyCfg()
    assert needs_sponsorship(p, "DE") is True
    assert needs_sponsorship(p, "GB") is True
    assert needs_sponsorship(p, "KZ") is False
    assert needs_sponsorship(p, "kg") is False
    assert needs_sponsorship(p, None) is None


def test_other_sensitive_topics_need_preapproved_answer():
    f = replace(CLEAN, topics_asked=frozenset({"notice_period"}))
    d = decide_apply(PolicyCfg(), f)
    assert d.route is Route.ASK and "no pre-approved answer" in d.reasons[0]
    ok = replace(f, topics_preapproved=frozenset({"notice_period"}))
    assert decide_apply(PolicyCfg(), ok).route is Route.SUBMIT


def test_apply_level_l1_asks():
    policy = PolicyCfg(autonomy={"apply": "L1"})
    assert decide_apply(policy, CLEAN).route is Route.ASK


def test_messages_default_to_approval_and_sensitive_always_asks():
    p = PolicyCfg()
    assert decide_message(p, "reply", set()).route is Route.ASK
    p2 = PolicyCfg(autonomy={"reply": "L2"})
    assert decide_message(p2, "reply", {"greeting"}).route is Route.SUBMIT
    d = decide_message(p2, "reply", {"compensation", "visa"})
    assert d.route is Route.ASK and "compensation, visa" in d.reasons[0]


def test_settings_reject_lease_shorter_than_hard_timeout():
    with pytest.raises(ValueError, match="lease_seconds"):
        Settings(queue={"lease_seconds": 300}, runner=RunnerCfg(hard_timeout_s=300))
    with pytest.raises(ValueError, match="max_seconds"):
        Settings(runner=RunnerCfg(max_seconds=300, hard_timeout_s=300))


# --- attention tags (ADR 0017) ------------------------------------------------------------


@pytest.mark.parametrize("entity", ["job", "application", "conversation"])
def test_every_state_has_exactly_one_tag(entity):
    table, _ = states.MACHINES[entity]
    assert set(states.ATTENTION[entity]) == set(table)


def test_attention_examples():
    A = states.Attention
    assert states.attention("application", "awaiting_approval") is A.NEEDS_HUMAN
    assert states.attention("application", "submit_unknown") is A.NEEDS_HUMAN
    assert states.attention("application", "rejected") is A.NOT_OK
    assert states.attention("application", "applied") is A.OK
    assert states.attention("job", "shortlisted") is A.OK
    assert states.attention("job", "shortlisted", protected=True) is A.NEEDS_HUMAN
    assert states.attention("side_effect", "unknown") is A.NEEDS_HUMAN
    assert states.states_tagged("job", A.NOT_OK) == {"dropped", "skipped", "closed"}


def test_terminal_states_are_never_ok_except_none():
    for entity in ("job", "application"):
        table, _ = states.MACHINES[entity]
        for s in states.terminal_states(table):
            assert states.attention(entity, s) is not states.Attention.OK, (entity, s)


def test_auto_apply_path_is_allowed():
    states.check_transition("application", "filling", "submitting")


# --- retention (ADR 0018) -----------------------------------------------------------------


def _job(conn, clock, jid, state_path, data):
    create_entity(conn, clock, "job", jid, data=data)
    for s in state_path:
        transition(conn, clock, "job", jid, s)


def test_retention_deletes_and_compacts(conn, clock, queue_cfg):
    q = Queue(conn, clock, queue_cfg)
    old_done, _ = q.enqueue("x")
    q.complete(q.lease("w").id)
    run = control.start_run(conn, clock, "run queue", pid=999999)
    control.finish_run(conn, clock, run, "ok")
    conn.execute(
        "INSERT INTO llm_calls(ts, task, provider, model, prompt_version, status, tokens_in, tokens_out) "
        "VALUES (?, 'ping', 'gemini', 'm', 'v1', 'ok', 10, 5)", ("2026-09-01T00:00:00.000000Z",))
    big = {"company": "IMC", "title": "C++ Dev", "url": "https://x", "fingerprint": "imc|c++ dev|ams",
           "source": "greenhouse", "description": "x" * 5000}
    _job(conn, clock, "dropped-job", ["dropped"], big)
    _job(conn, clock, "live-job", ["prefiltered", "labeled", "shortlisted"], big)
    se = control.SideEffects(conn, clock)
    se.plan("apply:imc:1", "submit", {"payload": "big"})
    se.mark("apply:imc:1", "done")
    se.plan("apply:imc:2", "submit")
    se.mark("apply:imc:2", "unknown")

    clock.advance(200 * 86400)
    new_done, _ = q.enqueue("x")
    q.complete(q.lease("w").id)
    cfg = RetentionCfg()

    preview = run_retention(conn, clock, cfg, dry_run=True)
    assert preview["tasks_done"] == 1 and preview["jobs_compacted"] == 1
    assert q.get(old_done) is not None  # dry run rolled back

    counts = run_retention(conn, clock, cfg)
    assert counts == preview
    assert q.get(old_done) is None and q.get(new_done) is not None
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0] == 0
    daily = conn.execute("SELECT calls, ok, tokens_in, tokens_out FROM llm_daily").fetchone()
    assert tuple(daily) == (1, 1, 10, 5)
    dropped = json.loads(conn.execute("SELECT data_json FROM jobs WHERE id='dropped-job'").fetchone()[0])
    assert dropped == {"company": "IMC", "title": "C++ Dev", "url": "https://x", "fingerprint": "imc|c++ dev|ams",
                       "source": "greenhouse", "resolution": "dropped", "compacted": True}
    live = json.loads(conn.execute("SELECT data_json FROM jobs WHERE id='live-job'").fetchone()[0])
    assert "description" in live  # not finished: untouched
    assert se.status("apply:imc:1") == "done"  # key kept for idempotency
    assert conn.execute("SELECT data_json FROM side_effects WHERE key='apply:imc:1'").fetchone()[0] == "{}"
    assert se.status("apply:imc:2") == "unknown"  # needs the owner: untouched
    job_events = conn.execute("SELECT COUNT(*) FROM events WHERE entity='job'").fetchone()[0]
    assert job_events == 0

    assert run_retention(conn, clock, cfg)["jobs_compacted"] == 0  # idempotent


def test_llm_daily_merges_a_day_split_across_two_retention_runs(conn, clock):
    """The cutoff can fall in the middle of a day: the morning is rolled up one night and the
    evening the next. Both must land in the same llm_daily row (ON CONFLICT path)."""
    insert = ("INSERT INTO llm_calls(ts, task, provider, model, prompt_version, status, tokens_in, tokens_out, "
              "cost_usd) VALUES (?, 'label', 'gemini', 'm', 'v1', ?, ?, ?, ?)")
    conn.execute(insert, ("2026-10-01T06:00:00.000000Z", "ok", 100, 10, 0.5))
    conn.execute(insert, ("2026-10-01T18:00:00.000000Z", "error", 50, 0, 0.25))
    cfg = RetentionCfg(llm_calls_days=35)
    clock.set(clock.now().replace(year=2026, month=11, day=5, hour=12))     # cutoff 2026-10-01 12:00
    first = run_retention(conn, clock, cfg)
    assert first["llm_calls"] == 1
    clock.advance(86400)                                                    # cutoff moves past 18:00
    second = run_retention(conn, clock, cfg)
    assert second["llm_calls"] == 1
    rows = conn.execute("SELECT day, calls, ok, tokens_in, tokens_out, cost_usd FROM llm_daily").fetchall()
    assert [tuple(r) for r in rows] == [("2026-10-01", 2, 1, 150, 10, 0.75)]


def test_compaction_marker_does_not_depend_on_json_formatting(conn, clock):
    _job(conn, clock, "j", ["dropped"], {})
    conn.execute("""UPDATE jobs SET data_json = '{"compacted":true,"resolution":"dropped"}' WHERE id = 'j'""")
    clock.advance(100 * 86400)
    assert run_retention(conn, clock, RetentionCfg())["jobs_compacted"] == 0
