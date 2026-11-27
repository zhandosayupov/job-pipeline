"""`jobpipe selfcheck` (S6): hourly checks that alert the owner when production breaks.

Each check is ok / failing with a reason. The owner is told once when a check starts failing and
once when it recovers (flags `selfcheck.<name>`), never every hour. When everything passes, an
optional dead-man's-switch URL is pinged (settings `selfcheck.ping_url`, e.g. a healthchecks.io
check): a VM that is down cannot send its own alert, the outside service does.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from ..adapters.storage.db import available_migrations, current_version
from ..clock import Clock, iso
from ..config.settings import AppConfig
from ..ports.notifier import Notifier
from . import control

LAST_RUN = "last_run."


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


def touch(conn: sqlite3.Connection, clock: Clock, command: str) -> None:
    """Record that `jobpipe run <command>` finished (no event row: the queue runs every minute)."""
    conn.execute("INSERT INTO control(key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE "
                 "SET value = excluded.value, updated_at = excluded.updated_at",
                 (LAST_RUN + command, iso(clock.now()), iso(clock.now())))


def _age_h(value: str | None, now: datetime) -> float | None:
    if not value:
        return None
    return (now - datetime.fromisoformat(value.replace("Z", "+00:00"))).total_seconds() / 3600


def gather(conn: sqlite3.Connection, clock: Clock, cfg: AppConfig) -> list[Check]:
    sc = cfg.settings.selfcheck
    now = clock.now()
    out: list[Check] = []

    try:
        conn.execute("INSERT INTO control(key, value, updated_at) VALUES ('selfcheck.write', '1', ?) "
                     "ON CONFLICT(key) DO UPDATE SET updated_at = excluded.updated_at", (iso(now),))
        v, latest = current_version(conn), len(available_migrations())
        out.append(Check("database", v == latest, "" if v == latest else f"schema {v}, code expects {latest}"))
    except sqlite3.Error as e:
        out.append(Check("database", False, f"{type(e).__name__}: {e}"))

    overdue = conn.execute("SELECT COUNT(*) FROM tasks WHERE status = 'pending' AND next_run_at < ?",
                           (iso(now - timedelta(minutes=sc.task_overdue_min)),)).fetchone()[0]
    out.append(Check("queue", overdue == 0, f"{overdue} task(s) overdue > {sc.task_overdue_min} min" if overdue
                     else ""))
    dead = conn.execute("SELECT COUNT(*) FROM tasks WHERE status = 'dead'").fetchone()[0]
    out.append(Check("dead_tasks", dead == 0, f"{dead} dead task(s): jobpipe task list --status dead" if dead
                     else ""))
    unknown = len(control.SideEffects(conn, clock).unknown())
    out.append(Check("side_effects", unknown == 0, f"{unknown} unknown outcome(s): jobpipe effects list"
                     if unknown else ""))

    for command, max_h in sc.max_age_hours.items():
        age = _age_h(control.get_flag(conn, LAST_RUN + command), now)
        if age is None:
            continue                       # never ran here: not deployed (yet)
        out.append(Check(f"timer.{command}", age <= max_h,
                         f"last `run {command}` {age:.1f} h ago (limit {max_h} h)" if age > max_h else ""))

    backups = sorted(Path(cfg.paths.data_dir, "backups").glob("jobpipe-*.db"))
    if backups or control.get_flag(conn, LAST_RUN + "backup"):
        if not backups:
            out.append(Check("backup", False, "no backup file"))
        else:
            b = backups[-1]
            age = (now.timestamp() - b.stat().st_mtime) / 3600
            good = _integrity(b)
            out.append(Check("backup", age <= 26 and good,
                             "" if age <= 26 and good else f"{b.name}: {age:.0f} h old, integrity {good}"))

    for wid, last in conn.execute("SELECT id, last_seen FROM workers").fetchall():
        age = _age_h(last, now)
        out.append(Check(f"worker.{wid}", age <= sc.worker_stale_hours,
                         f"last heartbeat {age:.1f} h ago" if age > sc.worker_stale_hours else ""))

    for src, finished, status in conn.execute(
            "SELECT s.source, s.finished_at, s.status FROM source_runs s JOIN (SELECT source, MAX(id) id FROM "
            "source_runs GROUP BY source) m ON m.id = s.id").fetchall():
        if src not in cfg.sources.sources or not cfg.sources.sources[src].enabled:
            continue
        age = _age_h(finished, now)
        ok = age is not None and age <= sc.source_stale_hours
        detail = "" if ok else (f"last run never finished ({status})" if age is None
                                else f"last fetched {age:.0f} h ago ({status})")
        out.append(Check(f"source.{src}", ok, detail))
    return out


def _integrity(path: Path) -> bool:
    try:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            return c.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        finally:
            c.close()
    except sqlite3.Error:
        return False


def run(conn: sqlite3.Connection, clock: Clock, cfg: AppConfig, notifier: Notifier, ping=None) -> list[Check]:
    checks = gather(conn, clock, cfg)
    names = {c.name for c in checks}
    for (key,) in conn.execute("SELECT key FROM control WHERE key LIKE 'selfcheck.%' AND value = 'fail'").fetchall():
        if key.removeprefix("selfcheck.") not in names:      # the check is gone (source disabled, worker removed)
            control.set_flag(conn, clock, key, "gone", actor="selfcheck")
    for c in checks:
        flag = f"selfcheck.{c.name}"
        was_failing = control.get_flag(conn, flag) == "fail"
        if not c.ok and not was_failing:
            if notifier.send(f"🚨 selfcheck: {c.name} — {c.detail}"):
                control.set_flag(conn, clock, flag, "fail", actor="selfcheck")
        elif c.ok and was_failing:
            notifier.send(f"✅ selfcheck: {c.name} снова в порядке.", silent=True)
            control.set_flag(conn, clock, flag, "ok", actor="selfcheck")
    url = cfg.settings.selfcheck.ping_url
    if url and all(c.ok for c in checks):
        (ping or _ping)(url)
    return checks


def _ping(url: str) -> None:
    import contextlib

    import httpx

    # A failed ping needs no handling: the outside service alerts when pings stop.
    with contextlib.suppress(httpx.HTTPError):
        httpx.get(url, timeout=10)
