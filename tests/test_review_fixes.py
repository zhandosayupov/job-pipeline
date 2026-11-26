"""Regression tests for the PR #1 review findings."""
import os
import sqlite3
import subprocess
import sys

import pytest

from jobpipe.adapters.notifier.telegram import RecordingNotifier
from jobpipe.adapters.storage.db import _split_sql, available_migrations, connect, migrate
from jobpipe.services import control
from jobpipe.services.queue import Queue
from jobpipe.workers.handlers import HANDLERS
from jobpipe.workers.runner import Runner


# 1. pid reuse must not make /pause kill an unrelated process
def test_process_identity_is_stable_for_a_live_process():
    ident = control.process_identity(os.getpid())
    if ident is None:
        pytest.skip("no process identity on this platform")
    assert ident == control.process_identity(os.getpid())


def test_stop_running_skips_a_reused_pid(conn, clock):
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        if control.process_identity(proc.pid) is None:
            pytest.skip("no process identity on this platform")
        run_id = control.start_run(conn, clock, "run queue", pid=proc.pid)
        conn.execute("UPDATE runs SET proc_start = 'someone-else' WHERE id = ?", (run_id,))
        report = control.stop_running(conn, clock, grace_s=1)
        assert proc.pid not in report.terminated and proc.pid in report.already_gone
        assert proc.poll() is None  # the innocent process is still alive
        assert conn.execute("SELECT status FROM runs WHERE id = ?", (run_id,)).fetchone()[0] == "failed"
    finally:
        proc.kill()


# 2. an undelivered "unknown outcome" alert is retried, and /status lists unknown keys
class FlakyNotifier(RecordingNotifier):
    def __init__(self, fail_times):
        super().__init__()
        self.fail_times = fail_times

    def send(self, text, *, silent=False):
        if self.fail_times:
            self.fail_times -= 1
            return False
        return super().send(text, silent=silent)


def _runner(app_cfg, clock, notifier, handlers=None):
    c = connect(app_cfg.paths.db_path)
    migrate(c, clock)
    return Runner(c, clock, app_cfg, handlers or HANDLERS, notifier), c


def test_unknown_alert_retried_until_delivered(app_cfg, clock):
    notifier = FlakyNotifier(fail_times=1)
    runner, conn = _runner(app_cfg, clock, notifier)
    dead = control.start_run(conn, clock, "run queue", pid=999999)
    control.finish_run(conn, clock, dead, "killed")
    control.SideEffects(conn, clock, run_id=dead).plan("apply:x:1", "submit")
    runner.run(max_seconds=5)
    assert notifier.messages == []  # Telegram down
    runner.run(max_seconds=5)
    assert len(notifier.messages) == 1 and "apply:x:1" in notifier.messages[0]
    runner.run(max_seconds=5)
    assert len(notifier.messages) == 1  # not repeated once delivered


def test_owner_resolves_unknown(conn, clock):
    se = control.SideEffects(conn, clock)
    se.plan("k", "submit")
    se.mark("k", "unknown")
    se.resolve("k", "reset")
    assert se.status("k") is None
    assert se.plan("k", "submit")  # may run again after reset
    with pytest.raises(KeyError):
        se.resolve("nope", "done")
    with pytest.raises(ValueError):
        se.resolve("k", "maybe")


# 3. poison pill: covered in test_queue; the runner alerts on it
def test_runner_alerts_on_reclaim_dead_letter(app_cfg, clock):
    notifier = RecordingNotifier()
    runner, conn = _runner(app_cfg, clock, notifier)
    q = Queue(conn, clock, app_cfg.settings.queue)
    tid, _ = q.enqueue("selftest.ok")
    crashes = app_cfg.settings.queue.max_reclaims
    for i in range(crashes):
        q.lease("crashing-runner")
        clock.advance(app_cfg.settings.queue.lease_seconds + 1)
        if i < crashes - 1:
            q.reclaim_expired()
    summary = runner.run(max_seconds=5)  # the last reclaim happens here and dead-letters the task
    assert summary.dead == 1 and q.get(tid).status == "dead"
    assert "lease expired" in notifier.messages[0]


# 4/5. SIGTERM only sets a flag; handlers stop cooperatively and the task is released
def test_stop_flag_releases_task_via_guard(app_cfg, clock):
    def long_task(task, ctx):
        runner._stop = True  # what the SIGTERM handler does
        ctx.guard()
        raise AssertionError("guard must stop the handler")

    runner, conn = _runner(app_cfg, clock, RecordingNotifier(), {"long": long_task})
    q = Queue(conn, clock, app_cfg.settings.queue)
    tid, _ = q.enqueue("long")
    summary = runner.run(max_seconds=5)
    assert summary.status == "stopped"
    t = q.get(tid)
    assert t.status == "pending" and t.attempts == 0
    assert conn.execute("SELECT status FROM runs").fetchone()[0] == "stopped"


# 7. migrations are split by SQLite's tokenizer
def test_split_sql_handles_triggers_and_semicolons_in_strings():
    sql = """
    -- comment
    CREATE TABLE a (x TEXT DEFAULT 'a;b');
    CREATE TRIGGER t AFTER INSERT ON a BEGIN
      UPDATE a SET x = 'c;d';
    END;
    -- trailing comment
    """
    stmts = _split_sql(sql)
    assert len(stmts) == 2 and stmts[1].startswith("CREATE TRIGGER") and stmts[1].endswith("END;")
    with pytest.raises(ValueError, match="incomplete"):
        _split_sql("CREATE TABLE b (x TEXT")


def test_all_migrations_split_cleanly():
    for _, name, sql in available_migrations():
        assert _split_sql(sql), name


def test_failed_commit_rolls_back_and_connection_stays_usable(conn):
    """A COMMIT that fails (here: a deferred foreign key) must not leave the transaction open."""
    from jobpipe.adapters.storage.db import tx

    conn.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TABLE child (pid INTEGER REFERENCES parent(id) DEFERRABLE INITIALLY DEFERRED)")
    with pytest.raises(sqlite3.IntegrityError), tx(conn):
        conn.execute("INSERT INTO child(pid) VALUES (42)")  # only checked at COMMIT
    assert not conn.in_transaction
    assert conn.execute("SELECT COUNT(*) FROM child").fetchone()[0] == 0
    with tx(conn):  # the next transaction works
        conn.execute("INSERT INTO parent(id) VALUES (1)")
    assert conn.execute("SELECT COUNT(*) FROM parent").fetchone()[0] == 1


def test_new_database_uses_incremental_auto_vacuum(conn):
    assert conn.execute("PRAGMA auto_vacuum").fetchone()[0] == 2
