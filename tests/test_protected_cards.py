"""S2 protected pool, shortlist cards, button actions, label rating, bot service commands."""
import json
from datetime import date

import yaml

from jobpipe.adapters.notifier.telegram import RecordingNotifier
from jobpipe.adapters.storage.db import open_db
from jobpipe.bot.app import BotService
from jobpipe.config.settings import ProtectedCfg, ProtectedEntry
from jobpipe.services import cards
from jobpipe.services import protected as prot
from jobpipe.services.events import create_entity, transition

from .conftest import REPO

POOL = ProtectedCfg(companies=[
    ProtectedEntry(name="IMC", aliases=["IMC Trading"], ats_tokens=["imc"], domains=["imc.com"]),
    ProtectedEntry(name="Old Co", until="2020-01-01"),
])


def test_pool_matches_name_alias_token_domain_and_respects_until():
    pool = prot.ProtectedPool(POOL, today=date(2026, 11, 27))
    assert pool.match("IMC Trading B.V.")
    assert pool.match("Someone", source="greenhouse", source_job_id="imc:123")
    assert pool.match("Someone", url="https://careers.imc.com/job/1")
    assert not pool.match("Someone", url="https://notimc.com/x")
    assert not pool.match("Old Co")
    assert not pool.match("Acme", "https://acme.io", "greenhouse", "acme:1")


def test_example_protected_yaml_loads():
    cfg = ProtectedCfg.model_validate(yaml.safe_load((REPO / "config.example" / "protected.yaml").read_text("utf-8")))
    pool = prot.ProtectedPool(cfg)
    assert pool.match("Hudson River Trading") and pool.match("x", source="greenhouse", source_job_id="wehrtyou:1")


def job(conn, clock, jid, company="Acme", state="shortlisted", score=85, protected=0, url=None):
    create_entity(conn, clock, "job", jid, columns={
        "source": "greenhouse", "kind": "ats", "company": company, "title": "C++ Engineer",
        "url": url or f"https://x/{jid}", "protected": protected,
    }, data={"label": {"score": score, "track": "hft", "visa": "explicit", "visa_evidence": "we sponsor",
                       "remote_scope": "n/a", "action": "apply_now", "reason": "fits",
                       "breakdown": {"level": 25, "stack": 25, "legal": 25, "comp": 2, "fresh": 8}}})
    for s in ("prefiltered", "labeled", "shortlisted", "skipped")[: ["prefiltered", "labeled", "shortlisted",
                                                                     "skipped"].index(state) + 1]:
        if s == "skipped":
            transition(conn, clock, "job", jid, "skipped")
        else:
            transition(conn, clock, "job", jid, s)
    return conn.execute("SELECT rowid FROM jobs WHERE id = ?", (jid,)).fetchone()[0]


def test_protect_and_unprotect_edit_yaml_and_mark_jobs(app_cfg, clock):
    conn = open_db(app_cfg.paths.db_path, clock)
    path = app_cfg.paths.config_dir / "protected.yaml"
    job(conn, clock, "gh:acme:1", company="Acme Corp")
    assert prot.protect(path, conn, clock, "Acme")
    assert not prot.protect(path, conn, clock, "acme")
    assert conn.execute("SELECT protected FROM jobs").fetchone()[0] == 1
    assert prot.load_file(path).companies[0].name == "Acme"
    assert prot.unprotect(path, conn, clock, "ACME")
    assert not prot.unprotect(path, conn, clock, "Acme")
    assert conn.execute("SELECT protected FROM jobs").fetchone()[0] == 0
    kinds = [r[0] for r in conn.execute("SELECT kind FROM events WHERE entity='protected'")]
    assert kinds == ["added", "removed"]


def test_cards_sent_once_above_threshold_or_protected(app_cfg, clock):
    conn = open_db(app_cfg.paths.db_path, clock)
    job(conn, clock, "a", score=90)
    job(conn, clock, "b", score=65)
    job(conn, clock, "c", score=20, protected=1, company="IMC")
    n = RecordingNotifier()
    assert cards.send_cards(conn, n, ["a", "b", "c"], min_score=80) == 2
    assert cards.send_cards(conn, n, ["a", "b", "c"], min_score=80) == 0
    assert "Оценка 90/100" in n.messages[0] and "«we sponsor»" in n.messages[0]
    assert n.buttons[0][0][0][1].startswith("j:go:")
    assert n.buttons[1][0][0][1].startswith("j:draft:") and n.messages[1].startswith("🛡")
    assert all(len(d.encode()) <= 64 for b in n.buttons for row in b for _, d in row)


def test_job_actions(app_cfg, clock):
    conn = open_db(app_cfg.paths.db_path, clock)
    r1 = job(conn, clock, "a")
    r2 = job(conn, clock, "b")
    r3 = job(conn, clock, "c", protected=1, company="IMC")
    assert "заявка создана" in cards.handle_job_action(conn, clock, app_cfg, "go", r1)
    assert "уже создана" in cards.handle_job_action(conn, clock, app_cfg, "go", r1)
    app = conn.execute("SELECT state, idempotency_key, data_json FROM applications").fetchone()
    assert app[0] == "draft" and app[1] == "apply:acme:a" and json.loads(app[2]) == {"manual": False}
    assert "пропущена" in cards.handle_job_action(conn, clock, app_cfg, "skip", r2)
    assert conn.execute("SELECT state FROM jobs WHERE id='b'").fetchone()[0] == "skipped"
    assert "только черновик" in cards.handle_job_action(conn, clock, app_cfg, "go", r3)
    assert "заявка создана" in cards.handle_job_action(conn, clock, app_cfg, "draft", r3)
    assert "добавлена в protected" in cards.handle_job_action(conn, clock, app_cfg, "prot", r1)
    assert "не найдена" in cards.handle_job_action(conn, clock, app_cfg, "go", 99999)


def test_rating_flow(app_cfg, clock):
    conn = open_db(app_cfg.paths.db_path, clock)
    rowid = job(conn, clock, "a")
    text, buttons = cards.next_rating_card(conn)
    assert "1/30" in text and buttons[0][0][1] == f"r:up:{rowid}"
    assert "1/1 (100%)" in cards.record_rating(conn, clock, rowid, "up")
    assert cards.next_rating_card(conn) is None


def test_bot_service_s2_commands(app_cfg, clock):
    conn = open_db(app_cfg.paths.db_path, clock)
    job(conn, clock, "a", company="Acme", score=91)
    conn.execute("INSERT INTO source_runs(source, started_at, status, fetched, new, kept, dropped) "
                 "VALUES ('hn', 'x', 'blocked', 0, 0, 0, 0)")
    svc = BotService(app_cfg, clock)
    assert "91 · Acme — C++ Engineer" in svc.shortlist_text()
    assert "shortlisted 1" in svc.funnel_text()
    assert "⚠️ hn: blocked" in svc.sources_text()
    assert "выключен" in svc.dryrun("hh", "off") and "Использование" in svc.dryrun("hh", "maybe")
    assert "добавлена" in svc.protect("Acme") and "убрана" in svc.unprotect("Acme")
    assert "Использование" in svc.protect("  ")


def test_cards_are_capped_per_run_best_first(tmp_path, clock):
    from jobpipe.adapters.notifier.telegram import RecordingNotifier
    from jobpipe.adapters.storage.db import open_db
    from jobpipe.services import cards
    from jobpipe.services.events import create_entity, transition

    conn = open_db(tmp_path / "c.db", clock)
    ids = []
    for i in range(15):
        jid = f"t:{i}"
        create_entity(conn, clock, "job", jid, columns={"source": "t", "kind": "ats", "company": f"Co{i}",
                                                       "title": "C++", "url": f"https://x/{i}", "protected": 1},
                      data={"label": {"score": i, "breakdown": {}}})
        for s in ("prefiltered", "labeled", "shortlisted"):
            transition(conn, clock, "job", jid, s)
        ids.append(jid)
    n = RecordingNotifier()
    assert cards.send_cards(conn, n, ids, 80, max_cards=10) == 10
    assert "Co14" in n.messages[0] and "ещё 5" in n.messages[-1]          # best score first, rest counted
    sent = [r[0] for r in conn.execute("SELECT id FROM jobs WHERE json_extract(data_json, '$.card_sent')")]
    assert "t:0" not in sent and len(sent) == 10
