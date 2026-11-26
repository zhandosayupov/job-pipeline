"""Collector and digest end to end on a real SQLite database with fake sources (no network)."""

import json
from typing import ClassVar

import pytest
import yaml

from jobpipe.adapters.notifier.telegram import RecordingNotifier
from jobpipe.adapters.sources.base import FetchOutcome
from jobpipe.adapters.storage.db import open_db
from jobpipe.config.settings import CompaniesCfg, FiltersCfg, SourceCfg, SourcesCfg
from jobpipe.domain.job import Posting
from jobpipe.services import control, digest
from jobpipe.services.collect import Collector

from .conftest import REPO


def posting(source="fake", kind="aggregator", jid="1", company="Acme", title="C++ Engineer", **kw):
    return Posting(
        source=source,
        kind=kind,
        source_job_id=jid,
        company=company,
        title=title,
        url=f"https://x/{source}/{jid}",
        description=kw.pop("description", "Low latency C++."),
        **kw,
    )


class Script:
    """A fake source whose next fetch returns a scripted outcome."""

    outcomes: ClassVar[dict[str, list]] = {}

    def __init__(self, name):
        self.name = name

    def fetch(self, http, ctx):
        nxt = self.outcomes[self.name].pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        if callable(nxt):
            return nxt(ctx)
        return nxt


def registry(*names):
    return {n: (lambda n=n: Script(n)) for n in names}


@pytest.fixture
def setup(app_cfg, clock):
    filters = FiltersCfg.model_validate(
        yaml.safe_load((REPO / "config.example" / "filters.yaml").read_text(encoding="utf-8"))
    )
    app_cfg.filters = filters
    app_cfg.companies = CompaniesCfg()
    conn = open_db(app_cfg.paths.db_path, clock)
    notifier = RecordingNotifier()

    def make(*names, alert_after=3):
        app_cfg.sources = SourcesCfg(
            sources={n: SourceCfg(enabled=True) for n in names}, health_alert_after=alert_after
        )
        return Collector(conn, clock, app_cfg, http=None, notifier=notifier, registry=registry(*names))

    return conn, notifier, make


def states(conn):
    return {r["id"]: (r["state"], r["drop_rule"]) for r in conn.execute("SELECT id, state, drop_rule FROM jobs")}


def test_new_postings_are_prefiltered_or_dropped_with_reason(setup):
    conn, _, make = setup
    Script.outcomes = {
        "fake": [
            FetchOutcome(
                postings=[
                    posting(jid="1"),
                    posting(jid="2", title="Sales Manager"),
                    posting(jid="3", title="C++ Intern"),
                ]
            )
        ]
    }
    [r] = make("fake").run()
    assert (r.status, r.fetched, r.new, r.kept, r.dropped) == ("ok", 3, 3, 1, 2)
    assert states(conn) == {
        "fake:1": ("prefiltered", None),
        "fake:2": ("dropped", "title_not_eng"),
        "fake:3": ("dropped", "title_excluded"),
    }
    data = json.loads(conn.execute("SELECT data_json FROM jobs WHERE id='fake:1'").fetchone()[0])
    assert {"excerpt", "visa", "remote", "lang"} <= set(data)
    events = conn.execute("SELECT COUNT(*) FROM events WHERE entity='job'").fetchone()[0]
    assert events == 6  # created + transition for each


def test_seen_posting_is_not_reinserted(setup):
    conn, _, make = setup
    Script.outcomes = {"fake": [FetchOutcome(postings=[posting()]), FetchOutcome(postings=[posting()])]}
    c = make("fake")
    c.run()
    [r] = c.run()
    assert (r.new, r.kept) == (0, 0)
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


def test_dedupe_prefers_ats_over_aggregator(setup):
    conn, _, make = setup
    Script.outcomes = {
        "agg": [FetchOutcome(postings=[posting(source="agg", kind="aggregator", location="Amsterdam")])],
        "ats": [
            FetchOutcome(postings=[posting(source="ats", kind="ats", company="Acme B.V.", location="Amsterdam, NL")])
        ],
    }
    make("agg", "ats").run()
    s = states(conn)
    assert s["ats:1"] == ("prefiltered", None)
    assert s["agg:1"] == ("dropped", "duplicate")
    dup = conn.execute("SELECT duplicate_of FROM jobs WHERE id='agg:1'").fetchone()[0]
    assert dup == "ats:1"


def test_dedupe_keeps_first_when_not_better(setup):
    conn, _, make = setup
    Script.outcomes = {
        "ats": [FetchOutcome(postings=[posting(source="ats", kind="ats")])],
        "agg": [FetchOutcome(postings=[posting(source="agg", kind="aggregator")])],
    }
    make("ats", "agg").run()
    assert states(conn)["agg:1"] == ("dropped", "duplicate")


def test_complete_board_closes_missing_jobs(setup):
    conn, _, make = setup
    first = FetchOutcome(
        postings=[posting(source="gh", jid="acme:1"), posting(source="gh", jid="acme:2", title="Go Developer")],
        complete_ids={"gh:acme": {"gh:acme:1", "gh:acme:2"}},
    )
    second = FetchOutcome(postings=[posting(source="gh", jid="acme:1")], complete_ids={"gh:acme": {"gh:acme:1"}})
    Script.outcomes = {"gh": [first, second]}
    c = make("gh")
    c.run()
    [r] = c.run()
    assert r.closed == 1 and states(conn)["gh:acme:2"][0] == "closed"


def test_source_failures_isolated_and_alerted_once(setup):
    conn, notifier, make = setup
    bad = FetchOutcome(errors=["unit: HTTP 500"])
    Script.outcomes = {
        "bad": [bad, bad, bad, bad, FetchOutcome()],
        "good": [FetchOutcome(postings=[posting(source="good")])] + [FetchOutcome()] * 4,
    }
    c = make("bad", "good", alert_after=3)
    for _ in range(5):
        c.run()
    assert states(conn)["good:1"][0] == "prefiltered"
    alerts = [m for m in notifier.messages if "bad" in m]
    assert len(alerts) == 2 and "3 запуска подряд" in alerts[0] and "снова работает" in alerts[1]


def test_crashing_source_is_recorded_as_error(setup):
    conn, _, make = setup
    Script.outcomes = {"boom": [RuntimeError("parser bug")]}
    [r] = make("boom").run()
    assert r.status == "error" and "parser bug" in r.errors[0]
    assert conn.execute("SELECT status FROM source_runs").fetchone()[0] == "error"


def test_cursors_persist(setup):
    conn, _, make = setup

    def with_cursor(ctx):
        assert ctx.cursors.get("last") in (None, "5")
        ctx.cursors["last"] = "5"
        return FetchOutcome()

    Script.outcomes = {"tg": [with_cursor, with_cursor]}
    c = make("tg")
    c.run()
    c.run()
    assert conn.execute("SELECT value FROM source_cursors WHERE source='tg'").fetchone()[0] == "5"


def test_paused_collect_does_nothing(setup, clock):
    conn, _, make = setup
    Script.outcomes = {"fake": [FetchOutcome(postings=[posting()])]}
    control.set_flag(conn, clock, control.PAUSED, "1")
    assert make("fake").run() == []


# --- digest ------------------------------------------------------------------------------


def test_digest_sends_each_job_once_and_orders_by_priority(setup, clock):
    conn, notifier, make = setup
    Script.outcomes = {
        "fake": [
            FetchOutcome(
                postings=[
                    posting(jid="1", title="Go Developer", company="Plain"),
                    posting(jid="2", title="C++ Engineer", company="Visa Co", description="We offer visa sponsorship."),
                ]
            )
        ]
    }
    make("fake").run()
    d = digest.send(conn, clock, notifier, "Asia/Almaty", max_items=40)
    assert d and len(d.job_ids) == 2
    text = notifier.messages[-1]
    assert text.index("Visa Co") < text.index("Plain") and "виза ✓" in text
    again = digest.send(conn, clock, notifier, "Asia/Almaty", max_items=40)
    assert again.job_ids == [] and "Новых вакансий нет" in notifier.messages[-1]


def test_failed_digest_send_leaves_jobs_pending(setup, clock):
    conn, _, make = setup
    Script.outcomes = {"fake": [FetchOutcome(postings=[posting()])]}
    make("fake").run()

    class Down(RecordingNotifier):
        def send(self, text, *, silent=False):
            return False

    assert digest.send(conn, clock, Down(), "Asia/Almaty", 40) is None
    assert conn.execute("SELECT digested_at FROM jobs").fetchone()[0] is None


def test_digest_splits_long_output_and_caps_items(setup, clock):
    conn, _, make = setup
    Script.outcomes = {
        "fake": [
            FetchOutcome(
                postings=[
                    posting(jid=str(i), company=f"Company {i}", title=f"C++ Engineer {i}", description="x " * 50)
                    for i in range(120)
                ]
            )
        ]
    }
    make("fake").run()
    d = digest.build(conn, clock, "Asia/Almaty", max_items=100)
    assert len(d.messages) > 1 and all(len(m) <= digest.MAX_MESSAGE for m in d.messages)
    assert "ещё 20" in d.messages[-1] and len(d.job_ids) == 120


def test_digest_groups_the_same_role_across_locations(setup, clock):
    conn, _, make = setup
    Script.outcomes = {
        "fake": [
            FetchOutcome(
                postings=[
                    posting(jid=str(i), company="think-cell", title="C++ Developer", location=loc)
                    for i, loc in enumerate(["Berlin", "Amsterdam", "Prague"])
                ]
            )
        ]
    }
    make("fake").run()
    d = digest.build(conn, clock, "Asia/Almaty", max_items=40)
    text = "\n".join(d.messages)
    assert text.count("think-cell — C++ Developer") == 1
    assert "ещё 2:" in text and len(d.job_ids) == 3
