import subprocess
import sys
import time

import pytest

from jobpipe.domain.errors import Paused
from jobpipe.services import control


def test_flags_and_events(conn, clock):
    assert not control.is_paused(conn)
    control.set_flag(conn, clock, control.PAUSED, "1")
    control.set_flag(conn, clock, control.PAUSED, "1")  # no-op, no second event
    assert control.is_paused(conn)
    with pytest.raises(Paused):
        control.ensure_not_paused(conn)
    n = conn.execute("SELECT COUNT(*) FROM events WHERE entity = 'control'").fetchone()[0]
    assert n == 1


def test_dry_run_defaults_on(conn, clock):
    assert control.is_dry_run(conn, "hh")
    control.set_flag(conn, clock, "dry_run.hh", "0")
    assert not control.is_dry_run(conn, "hh")
    assert control.is_dry_run(conn, "greenhouse")


def test_side_effect_key_is_reserved_once(conn, clock):
    se = control.SideEffects(conn, clock)
    assert se.plan("apply:x:1", "submit")
    assert not se.plan("apply:x:1", "submit")
    se.mark("apply:x:1", "done")
    assert not se.plan("apply:x:1", "submit")
    assert se.status("apply:x:1") == "done"


def test_side_effect_refused_while_paused(conn, clock):
    control.set_flag(conn, clock, control.PAUSED, "1")
    with pytest.raises(Paused):
        control.SideEffects(conn, clock).plan("k", "submit")


def test_running_side_effect_is_not_orphaned(conn, clock):
    live = control.start_run(conn, clock, "run queue")
    control.SideEffects(conn, clock, run_id=live).plan("k", "submit")
    assert control.SideEffects(conn, clock).orphaned_to_unknown() == []


def test_reap_stale_runs(conn, clock):
    control.start_run(conn, clock, "run queue", pid=999999)
    assert control.reap_stale_runs(conn, clock) == 1
    assert conn.execute("SELECT status FROM runs").fetchone()[0] == "failed"


def test_stop_running_terminates_a_real_process_within_grace(conn, clock):
    """S0 done-condition: /pause stops a running job in <= 10 s."""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        run_id = control.start_run(conn, clock, "run queue", pid=proc.pid)
        t0 = time.monotonic()
        report = control.stop_running(conn, clock, grace_s=8)
        proc.wait(timeout=10)
        assert time.monotonic() - t0 < 10
        assert proc.pid in report.terminated
        status = conn.execute("SELECT status FROM runs WHERE id = ?", (run_id,)).fetchone()[0]
        assert status == "killed"
    finally:
        if proc.poll() is None:
            proc.kill()
