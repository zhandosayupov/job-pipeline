"""S6: selfcheck alerts once per failure and once on recovery; dead-man ping; backup restore test;
reply L2 is earned by clean approvals."""
from jobpipe.adapters.notifier.telegram import RecordingNotifier
from jobpipe.adapters.storage.db import open_db
from jobpipe.config.settings import PolicyCfg
from jobpipe.services import control, selfcheck
from jobpipe.services.backup import backup_db, verify_restore
from jobpipe.services.queue import Queue


def names(checks, ok=None):
    return {c.name for c in checks if ok is None or c.ok is ok}


def test_fresh_system_is_ok_and_pings(app_cfg, clock):
    conn = open_db(app_cfg.paths.db_path, clock)
    app_cfg.settings.selfcheck.ping_url = "https://hc.example/ping/x"
    pings = []
    checks = selfcheck.run(conn, clock, app_cfg, RecordingNotifier(), ping=pings.append)
    assert all(c.ok for c in checks) and {"database", "queue", "dead_tasks", "side_effects"} <= names(checks)
    assert pings == ["https://hc.example/ping/x"]


def test_alert_once_then_recovery(app_cfg, clock):
    conn = open_db(app_cfg.paths.db_path, clock)
    n = RecordingNotifier()
    selfcheck.touch(conn, clock, "collect")
    clock.advance(11 * 3600)
    q = Queue(conn, clock, app_cfg.settings.queue)
    q.enqueue("selftest.ok")
    clock.advance(31 * 60)
    pings = []
    checks = selfcheck.run(conn, clock, app_cfg, n, ping=pings.append)
    assert {"timer.collect", "queue"} <= names(checks, ok=False) and pings == []
    first = len(n.messages)
    assert first == 2 and all("🚨" in m for m in n.messages)
    selfcheck.run(conn, clock, app_cfg, n)
    assert len(n.messages) == first                       # no repeat every hour
    selfcheck.touch(conn, clock, "collect")
    conn.execute("UPDATE tasks SET status = 'done'")
    selfcheck.run(conn, clock, app_cfg, n)
    assert sum("снова в порядке" in m for m in n.messages) == 2


def test_stale_worker_dead_task_and_unknown_effect(app_cfg, clock):
    conn = open_db(app_cfg.paths.db_path, clock)
    conn.execute("INSERT INTO workers(id, last_seen) VALUES ('laptop', '2026-11-24T00:00:00Z')")
    q = Queue(conn, clock, app_cfg.settings.queue)
    tid, _ = q.enqueue("x", max_attempts=1)
    q.lease("t", ["x"])
    q.fail(tid, "boom")
    se = control.SideEffects(conn, clock)
    se.plan("k", "apply")
    se.mark("k", "unknown")
    bad = names(selfcheck.gather(conn, clock, app_cfg), ok=False)
    assert {"worker.laptop", "dead_tasks", "side_effects"} <= bad


def test_backup_check_and_restore_test(app_cfg, clock, tmp_path):
    conn = open_db(app_cfg.paths.db_path, clock)
    conn.execute("INSERT INTO control(key, value, updated_at) VALUES ('x', '1', 'now')")
    b = backup_db(conn, app_cfg.paths.data_dir / "backups", clock)
    counts = verify_restore(b, tmp_path / "restore")
    assert counts["jobs"] == 0 and counts["schema"] >= 4
    assert not list((tmp_path / "restore").glob("*.db"))          # the test copy is removed
    assert "backup" in names(selfcheck.gather(conn, clock, app_cfg), ok=True)
    b.write_bytes(b"not a database")
    assert "backup" in names(selfcheck.gather(conn, clock, app_cfg), ok=False)


def test_reply_l2_needs_clean_approvals(app_cfg, clock):
    from .test_inbox_flow import FakeMailbox, add_app, run  # the S5 harness

    conn = open_db(app_cfg.paths.db_path, clock)
    add_app(conn, clock, "Acme", "C++ Engineer")
    app_cfg.policy = PolicyCfg(autonomy={"apply": "L2", "reply": "L2", "email_draft": "L1"},
                               sensitive_topics=[])
    mb = FakeMailbox()
    mb.add("inbox", (b"From: Jane <jane@acme.example>\r\nTo: alex@example.com\r\nSubject: Acme role\r\n"
                     b"Message-ID: <q1@acme.example>\r\nDate: Wed, 25 Nov 2026 10:00:00 +0000\r\n\r\n"
                     b"Hi, could you share your GitHub profile?\r\n"), "T1")
    n = RecordingNotifier()
    run(conn, clock, app_cfg, mb, n, Queue(conn, clock, app_cfg.settings.queue))
    card = next(m for m, b in zip(n.messages, n.buttons, strict=True) if b)
    assert "L2 for replies needs 20 drafts sent unedited, so far 0" in card


def test_digest_carries_funnel_metrics(app_cfg, clock):
    from jobpipe.services import digest

    from .test_inbox_flow import add_app

    conn = open_db(app_cfg.paths.db_path, clock)
    add_app(conn, clock, "Acme", "C++ Engineer")
    d = digest.build(conn, clock, "Asia/Almaty", 40)
    assert "Воронка: подано 1" in d.messages[0]


def test_unfinished_source_run_is_a_failing_check_not_a_crash(app_cfg, clock):
    from jobpipe.config.settings import SourceCfg, SourcesCfg

    conn = open_db(app_cfg.paths.db_path, clock)
    app_cfg.sources = SourcesCfg(sources={"himalayas": SourceCfg(enabled=True)})
    conn.execute("INSERT INTO source_runs(source, started_at, status) VALUES ('himalayas', '2026-11-26T05:00:00Z', "
                 "'running')")
    bad = {c.name: c for c in selfcheck.gather(conn, clock, app_cfg) if not c.ok}
    assert "never finished" in bad["source.himalayas"].detail


def test_flag_of_a_vanished_check_is_cleared(app_cfg, clock):
    conn = open_db(app_cfg.paths.db_path, clock)
    control.set_flag(conn, clock, "selfcheck.worker.old-laptop", "fail")
    selfcheck.run(conn, clock, app_cfg, RecordingNotifier())
    assert control.get_flag(conn, "selfcheck.worker.old-laptop") == "gone"
