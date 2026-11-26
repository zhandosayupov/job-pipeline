"""/status: gather a snapshot (I/O) and render it (pure), so rendering is unit-tested."""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from .. import __version__
from ..adapters.storage.db import current_version
from ..clock import Clock, iso, parse_iso
from ..domain import states
from ..services import control
from ..services.llm_router import provider_status
from ..services.queue import Queue


@dataclass
class StatusSnapshot:
    version: str
    now: datetime
    tz: str
    paused: bool
    schema_version: int
    tasks: dict[str, int]
    due: int
    dead: list[tuple[int, str, str]]
    running: list[tuple[str, int, str]]
    last_runs: list[tuple[str, str, str]]
    llm_today: list[dict]
    providers: dict[str, str]
    db_size_kb: int
    unknown_effects: list[str] = field(default_factory=list)
    needs_human: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def needs_human_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """How many items per kind wait for the owner (attention tag needs_human, ADR 0017)."""
    out: dict[str, int] = {}
    for entity, table in (("job", "jobs"), ("application", "applications"), ("conversation", "conversations")):
        tagged = sorted(states.states_tagged(entity, states.Attention.NEEDS_HUMAN))
        clause = f"state IN ({', '.join('?' for _ in tagged)})" if tagged else "0"
        extra = " OR (state = 'shortlisted' AND protected = 1)" if entity == "job" else ""
        n = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {clause}{extra}", tagged).fetchone()[0]
        if n:
            out[entity] = n
    for entity, table, col in (("task", "tasks", "status"), ("side_effect", "side_effects", "status")):
        tagged = sorted(states.states_tagged(entity, states.Attention.NEEDS_HUMAN))
        n = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {col} IN ({', '.join('?' for _ in tagged)})",
                         tagged).fetchone()[0]
        if n:
            out[entity] = n
    return out


def gather(conn: sqlite3.Connection, clock: Clock, queue: Queue, tz: str, db_path: str | None) -> StatusSnapshot:
    tasks = queue.counts()
    running = [(r["command"], r["pid"], r["started_at"]) for r in control.running_runs(conn)]
    last = conn.execute(
        "SELECT command, status, COALESCE(finished_at, started_at) AS at FROM runs "
        "WHERE status != 'running' ORDER BY id DESC LIMIT 3"
    ).fetchall()
    day_start = clock.now().astimezone(ZoneInfo(tz)).replace(hour=0, minute=0, second=0, microsecond=0)
    llm = conn.execute(
        "SELECT provider, model, COUNT(*) AS calls, SUM(status = 'ok') AS ok, "
        "COALESCE(SUM(tokens_in + tokens_out), 0) AS tokens FROM llm_calls WHERE ts >= ? "
        "GROUP BY provider, model ORDER BY provider, calls DESC",
        (iso(day_start),),
    ).fetchall()
    size = 0
    if db_path and os.path.exists(db_path):
        size = os.path.getsize(db_path) // 1024
    return StatusSnapshot(
        version=__version__,
        now=clock.now(),
        tz=tz,
        paused=control.is_paused(conn),
        schema_version=current_version(conn),
        tasks=tasks,
        due=queue.due_count(),
        dead=[(t.id, t.type, t.last_error or "") for t in queue.dead(3)],
        running=running,
        last_runs=[(r["command"], r["status"], r["at"]) for r in last],
        llm_today=[dict(r) for r in llm],
        providers=provider_status(conn, clock),
        db_size_kb=size,
        unknown_effects=control.SideEffects(conn, clock).unknown(),
        needs_human=needs_human_counts(conn),
    )


def _target(row: dict) -> str:
    return f"{row['provider']}/{row['model']}" if row.get("model") else row["provider"]


def _local(ts: str, tz: str) -> str:
    return parse_iso(ts).astimezone(ZoneInfo(tz)).strftime("%d.%m %H:%M")


def render(s: StatusSnapshot) -> str:
    now_local = s.now.astimezone(ZoneInfo(s.tz)).strftime("%d.%m %H:%M")
    lines = [
        f"jobpipe {s.version} · {now_local} ({s.tz})",
        "Пауза: ВКЛЮЧЕНА — внешние действия остановлены" if s.paused else "Пауза: нет",
        "",
        "Очередь: "
        + (", ".join(f"{k} {v}" for k, v in sorted(s.tasks.items())) if s.tasks else "пусто")
        + (f" · к запуску сейчас {s.due}" if s.due else ""),
    ]
    if s.needs_human:
        names = {"job": "вакансии", "application": "заявки", "conversation": "переписки",
                 "task": "dead-задачи", "side_effect": "неизвестный исход"}
        lines.insert(2, "Ждёт тебя: " + ", ".join(f"{names.get(k, k)} {v}" for k, v in s.needs_human.items()))
    if s.unknown_effects:
        lines.append("Исход неизвестен (реши: jobpipe effects resolve KEY done|failed|reset):")
        lines += [f"  {k}" for k in s.unknown_effects[:10]]
    if s.dead:
        lines.append("Dead-letter (последние):")
        lines += [f"  #{i} {t}: {err[:80]}" for i, t, err in s.dead]
    if s.running:
        lines.append("Сейчас работает:")
        lines += [f"  {cmd} (pid {pid}) с {_local(at, s.tz)}" for cmd, pid, at in s.running]
    if s.last_runs:
        lines.append("Последние запуски:")
        lines += [f"  {cmd}: {st}, {_local(at, s.tz)}" for cmd, st, at in s.last_runs]
    lines.append("")
    if s.llm_today:
        lines.append("LLM сегодня:")
        lines += [f"  {_target(r)}: {r['calls']} вызовов, ok {r['ok']}, {r['tokens']} токенов" for r in s.llm_today]
    else:
        lines.append("LLM сегодня: вызовов не было")
    for name, state in sorted(s.providers.items()):
        lines.append(f"  {name}: {state}")
    lines.append(f"\nБД: схема v{s.schema_version}, {s.db_size_kb} КБ")
    lines += s.notes
    return "\n".join(lines)
