"""Kill switch, run registry and side-effect journal (ADR 0014)."""
from __future__ import annotations

import json
import os
import signal
import socket
import sqlite3
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..adapters.storage.db import tx
from ..clock import Clock, iso
from ..domain.errors import Paused
from .events import record_event

PAUSED = "paused"


# --- switches ------------------------------------------------------------------------


def get_flag(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM control WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_flag(conn: sqlite3.Connection, clock: Clock, key: str, value: str, actor: str = "owner") -> None:
    with tx(conn):
        old = get_flag(conn, key)
        conn.execute(
            "INSERT INTO control(key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, value, iso(clock.now())),
        )
        if old != value:
            record_event(conn, clock, entity="control", entity_id=key, kind="set", actor=actor,
                         from_state=old, to_state=value)


def is_paused(conn: sqlite3.Connection) -> bool:
    return get_flag(conn, PAUSED, "0") == "1"


def ensure_not_paused(conn: sqlite3.Connection) -> None:
    if is_paused(conn):
        raise Paused("kill switch is on")


def is_dry_run(conn: sqlite3.Connection, platform: str) -> bool:
    """DRY_RUN is on unless the owner explicitly turned it off for this platform."""
    return get_flag(conn, f"dry_run.{platform}", "1") != "0"


# --- run registry --------------------------------------------------------------------


def start_run(conn: sqlite3.Connection, clock: Clock, command: str, pid: int | None = None) -> int:
    pid = pid or os.getpid()
    with tx(conn):
        cur = conn.execute(
            "INSERT INTO runs(command, pid, proc_start, host, status, started_at) VALUES (?, ?, ?, ?, 'running', ?)",
            (command, pid, process_identity(pid), socket.gethostname(), iso(clock.now())),
        )
    return cur.lastrowid


def finish_run(conn: sqlite3.Connection, clock: Clock, run_id: int, status: str,
               info: dict[str, Any] | None = None) -> None:
    with tx(conn):
        conn.execute(
            "UPDATE runs SET status = ?, finished_at = ?, info_json = ? WHERE id = ? AND status = 'running'",
            (status, iso(clock.now()), json.dumps(info or {}, ensure_ascii=False), run_id),
        )


def running_runs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM runs WHERE status = 'running' AND host = ? ORDER BY id", (socket.gethostname(),)
    ).fetchall()


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        process_query_limited_information = 0x1000
        still_active = 259
        handle = ctypes.windll.kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return bool(ok) and code.value == still_active
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def process_identity(pid: int) -> str | None:
    """A token that changes when a pid is reused: boot id + start time on Linux, creation time
    on Windows. None where neither is available (then only liveness is checked)."""
    if sys.platform.startswith("linux"):
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
            # Field 2 (comm) may contain spaces and parens; fields after the last ')' start at 3.
            start_ticks = stat[stat.rindex(")") + 2 :].split()[22 - 3]
            boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            return f"{boot_id}:{start_ticks}"
        except (OSError, ValueError, IndexError):
            return None
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return None
        try:
            times = [wintypes.FILETIME() for _ in range(4)]
            if not ctypes.windll.kernel32.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
                return None
            created = times[0]
            return f"win:{(created.dwHighDateTime << 32) | created.dwLowDateTime}"
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    return None


def is_same_process(row: sqlite3.Row) -> bool:
    """True if the recorded run's process still exists and is the same process (not a reused pid)."""
    pid = row["pid"]
    if not pid_alive(pid):
        return False
    recorded = row["proc_start"]
    if recorded is None:
        return True
    return process_identity(pid) == recorded


@dataclass
class KillReport:
    terminated: list[int]
    killed: list[int]
    already_gone: list[int]
    elapsed_s: float


def reap_stale_runs(conn: sqlite3.Connection, clock: Clock) -> int:
    """Mark `running` rows whose process is gone (crash, reboot, pid reused) as failed."""
    n = 0
    for row in running_runs(conn):
        if not is_same_process(row):
            finish_run(conn, clock, row["id"], "failed", {"reason": "process vanished"})
            n += 1
    return n


def stop_running(
    conn: sqlite3.Connection,
    clock: Clock,
    *,
    grace_s: float = 8.0,
    exclude_pid: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> KillReport:
    """SIGTERM every recorded running process on this host; SIGKILL survivors after grace_s."""
    t0 = time.monotonic()
    exclude_pid = exclude_pid or os.getpid()
    terminated, gone = [], []
    targets: list[tuple[int, int]] = []
    for row in running_runs(conn):
        pid = row["pid"]
        if pid == exclude_pid:
            continue
        if not is_same_process(row):  # never signal a pid that now belongs to someone else
            gone.append(pid)
            finish_run(conn, clock, row["id"], "failed", {"reason": "process vanished"})
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            terminated.append(pid)
            targets.append((row["id"], pid))
        except ProcessLookupError:
            gone.append(pid)
            finish_run(conn, clock, row["id"], "failed", {"reason": "process vanished"})
    deadline = time.monotonic() + grace_s
    while targets and time.monotonic() < deadline:
        if all(not pid_alive(pid) for _, pid in targets):
            break
        sleep(0.2)
    killed = []
    for run_id, pid in targets:
        if pid_alive(pid):
            sigkill = getattr(signal, "SIGKILL", signal.SIGTERM)
            try:
                os.kill(pid, sigkill)
                killed.append(pid)
            except ProcessLookupError:
                pass
        finish_run(conn, clock, run_id, "killed", {"reason": "kill switch"})
    return KillReport(terminated, killed, gone, round(time.monotonic() - t0, 2))


# --- side-effect journal ---------------------------------------------------------------


class SideEffects:
    """Write the key before acting; a `planned` key found later means the outcome is unknown."""

    def __init__(self, conn: sqlite3.Connection, clock: Clock, run_id: int | None = None):
        self.conn = conn
        self.clock = clock
        self.run_id = run_id

    def plan(self, key: str, kind: str, data: dict[str, Any] | None = None) -> bool:
        """Reserve the key. Returns False if it already exists in any status (do not act again)."""
        ensure_not_paused(self.conn)
        now = iso(self.clock.now())
        with tx(self.conn):
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO side_effects(key, kind, status, run_id, data_json, created_at, "
                "updated_at) VALUES (?, ?, 'planned', ?, ?, ?, ?)",
                (key, kind, self.run_id, json.dumps(data or {}, ensure_ascii=False), now, now),
            )
        return cur.rowcount == 1

    def mark(self, key: str, status: str) -> None:
        if status not in {"done", "failed", "unknown"}:
            raise ValueError(status)
        with tx(self.conn):
            self.conn.execute(
                "UPDATE side_effects SET status = ?, updated_at = ? WHERE key = ?",
                (status, iso(self.clock.now()), key),
            )

    def status(self, key: str) -> str | None:
        row = self.conn.execute("SELECT status FROM side_effects WHERE key = ?", (key,)).fetchone()
        return row["status"] if row else None

    def orphaned_to_unknown(self) -> list[str]:
        """A `planned` key whose run is no longer running was interrupted mid-action. Mark it
        `unknown` for the owner to resolve; never retry it automatically."""
        with tx(self.conn):
            rows = self.conn.execute(
                "SELECT s.key FROM side_effects s LEFT JOIN runs r ON r.id = s.run_id "
                "WHERE s.status = 'planned' AND (r.id IS NULL OR r.status != 'running')"
            ).fetchall()
            keys = [r["key"] for r in rows]
            for k in keys:
                self.conn.execute(
                    "UPDATE side_effects SET status = 'unknown', updated_at = ? WHERE key = ?",
                    (iso(self.clock.now()), k),
                )
                record_event(self.conn, self.clock, entity="side_effect", entity_id=k, kind="transition",
                             from_state="planned", to_state="unknown")
        return keys

    def unknown(self, *, only_unnotified: bool = False) -> list[str]:
        q = "SELECT key FROM side_effects WHERE status = 'unknown'"
        if only_unnotified:
            q += " AND notified_at IS NULL"
        return [r["key"] for r in self.conn.execute(q + " ORDER BY updated_at").fetchall()]

    def mark_notified(self, keys: list[str]) -> None:
        with tx(self.conn):
            for k in keys:
                self.conn.execute("UPDATE side_effects SET notified_at = ? WHERE key = ?",
                                  (iso(self.clock.now()), k))

    def resolve(self, key: str, outcome: str, actor: str = "owner") -> None:
        """Owner's decision on an `unknown` or `failed` key: `done` (it happened), `failed` (it did
        not; keep blocked), or `reset` (delete the key so the action may run again)."""
        if outcome not in {"done", "failed", "reset"}:
            raise ValueError(outcome)
        with tx(self.conn):
            row = self.conn.execute("SELECT status FROM side_effects WHERE key = ?", (key,)).fetchone()
            if row is None:
                raise KeyError(key)
            if outcome == "reset":
                self.conn.execute("DELETE FROM side_effects WHERE key = ?", (key,))
            else:
                self.conn.execute("UPDATE side_effects SET status = ?, updated_at = ? WHERE key = ?",
                                  (outcome, iso(self.clock.now()), key))
            record_event(self.conn, self.clock, entity="side_effect", entity_id=key, kind="resolved",
                         actor=actor, from_state=row["status"], to_state=outcome)
