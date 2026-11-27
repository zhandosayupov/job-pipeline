"""S0 done-condition: a failing task retries with backoff and dead-letters with an alert."""
from jobpipe.adapters.notifier.telegram import RecordingNotifier
from jobpipe.adapters.storage.db import connect
from jobpipe.domain.errors import Paused, Permanent
from jobpipe.services import control
from jobpipe.services.queue import Queue
from jobpipe.workers.handlers import HANDLERS
from jobpipe.workers.runner import Runner


def make_runner(app_cfg, clock, handlers=None):
    conn = connect(app_cfg.paths.db_path)
    from jobpipe.adapters.storage.db import migrate

    migrate(conn, clock)
    notifier = RecordingNotifier()
    return Runner(conn, clock, app_cfg, handlers or HANDLERS, notifier), conn, notifier


def test_failing_task_retries_then_dead_letters_with_alert(app_cfg, clock):
    runner, conn, notifier = make_runner(app_cfg, clock)
    q = Queue(conn, clock, app_cfg.settings.queue)
    tid, _ = q.enqueue("selftest.fail")  # max_attempts 3 from the fixture config
    delays = []
    for _ in range(3):
        summary = runner.run(max_seconds=5)
        assert summary.status == "ok"
        t = q.get(tid)
        if t.status == "pending":
            delays.append(t.next_run_at)
            clock.advance(200)  # past the backoff cap
    t = q.get(tid)
    assert t.status == "dead" and t.attempts == 3
    assert "selftest failure on attempt 3" in t.last_error
    assert len(delays) == 2 and delays[0] < delays[1]
    assert len(notifier.messages) == 1 and "dead-letter" in notifier.messages[0]
    runs = conn.execute("SELECT status FROM runs").fetchall()
    assert [r[0] for r in runs] == ["ok", "ok", "ok"]


def test_ok_task_completes(app_cfg, clock):
    runner, conn, _ = make_runner(app_cfg, clock)
    q = Queue(conn, clock, app_cfg.settings.queue)
    tid, _ = q.enqueue("selftest.ok")
    assert runner.run(max_seconds=5).done == 1
    assert q.get(tid).status == "done"


def test_runner_takes_only_types_it_can_run(app_cfg, clock):
    """Worker tasks (apply.fill, apply.submit) belong to the laptop: the VM queue run must not take
    them, or it would dead-letter every application (bug found in S5)."""
    runner, conn, notifier = make_runner(app_cfg, clock)
    q = Queue(conn, clock, app_cfg.settings.queue)
    fill, _ = q.enqueue("apply.fill", {"app_id": "a"})
    other, _ = q.enqueue("nope")
    ok, _ = q.enqueue("selftest.ok")
    summary = runner.run(max_seconds=5)
    assert summary.done == 1 and summary.dead == 0
    assert q.get(fill).status == "pending" and q.get(other).status == "pending"
    assert q.get(ok).status == "done"
    assert not notifier.messages


def test_unknown_type_dead_letters_when_asked_for_explicitly(app_cfg, clock):
    runner, conn, notifier = make_runner(app_cfg, clock)
    q = Queue(conn, clock, app_cfg.settings.queue)
    tid, _ = q.enqueue("nope")
    assert runner.run(max_seconds=5, types=["nope"]).dead == 1
    assert "no handler" in q.get(tid).last_error
    assert notifier.messages


def test_permanent_error(app_cfg, clock):
    def bad(task, ctx):
        raise Permanent("invalid payload")

    runner, conn, _ = make_runner(app_cfg, clock, {"bad": bad})
    q = Queue(conn, clock, app_cfg.settings.queue)
    tid, _ = q.enqueue("bad")
    runner.run(max_seconds=5)
    assert q.get(tid).status == "dead" and q.get(tid).attempts == 1


def test_paused_run_does_nothing(app_cfg, clock):
    runner, conn, _ = make_runner(app_cfg, clock)
    q = Queue(conn, clock, app_cfg.settings.queue)
    tid, _ = q.enqueue("selftest.ok")
    control.set_flag(conn, clock, control.PAUSED, "1")
    assert runner.run(max_seconds=5).status == "paused"
    assert q.get(tid).status == "pending"
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


def test_guard_inside_handler_releases_task(app_cfg, clock):
    calls = []

    def side_effecting(task, ctx):
        calls.append(task.id)
        control.set_flag(ctx.conn, ctx.clock, control.PAUSED, "1")  # owner pressed /pause meanwhile
        ctx.guard()
        raise AssertionError("must not get here")

    runner, conn, _ = make_runner(app_cfg, clock, {"se": side_effecting})
    q = Queue(conn, clock, app_cfg.settings.queue)
    a, _ = q.enqueue("se")
    b, _ = q.enqueue("se")
    runner.run(max_seconds=5)
    assert calls == [a]
    assert q.get(a).status == "pending" and q.get(a).attempts == 0
    assert q.get(b).status == "pending"


def test_interrupted_side_effect_becomes_unknown_and_is_reported(app_cfg, clock):
    runner, conn, notifier = make_runner(app_cfg, clock)
    dead_run = control.start_run(conn, clock, "run queue", pid=999999)
    control.finish_run(conn, clock, dead_run, "killed")
    control.SideEffects(conn, clock, run_id=dead_run).plan("apply:imc:123", "submit")
    summary = runner.run(max_seconds=5)
    assert summary.unknown_side_effects == ["apply:imc:123"]
    assert control.SideEffects(conn, clock).status("apply:imc:123") == "unknown"
    assert "apply:imc:123" in notifier.messages[0]


def test_paused_exception_type_is_domain_error():
    assert issubclass(Paused, Exception)
