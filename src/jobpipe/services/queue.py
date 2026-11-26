"""Task queue in SQLite (ADR 0003): leases, exponential backoff with jitter, dead-letter,
idempotency keys."""
from __future__ import annotations

import json
import random
import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from ..adapters.storage.db import tx
from ..clock import Clock, iso
from ..config.settings import QueueCfg
from .events import record_event


def backoff_delay(attempt: int, base: float, cap: float, rng: random.Random) -> float:
    """Equal-jitter exponential backoff for the given 1-based attempt number."""
    if attempt < 1:
        raise ValueError("attempt is 1-based")
    d = min(cap, base * (2 ** (attempt - 1)))
    return d / 2 + rng.uniform(0, d / 2)


@dataclass(frozen=True)
class Task:
    id: int
    type: str
    payload: dict[str, Any]
    status: str
    attempts: int
    max_attempts: int
    next_run_at: str
    idempotency_key: str | None
    last_error: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Task:
        return cls(
            id=row["id"],
            type=row["type"],
            payload=json.loads(row["payload_json"]),
            status=row["status"],
            attempts=row["attempts"],
            max_attempts=row["max_attempts"],
            next_run_at=row["next_run_at"],
            idempotency_key=row["idempotency_key"],
            last_error=row["last_error"],
        )


@dataclass(frozen=True)
class FailOutcome:
    dead: bool
    retry_in_s: float | None


class Queue:
    def __init__(self, conn: sqlite3.Connection, clock: Clock, cfg: QueueCfg, rng: random.Random | None = None):
        self.conn = conn
        self.clock = clock
        self.cfg = cfg
        self.rng = rng or random.Random()

    # --- producing -------------------------------------------------------------------

    def enqueue(
        self,
        task_type: str,
        payload: dict[str, Any] | None = None,
        *,
        idempotency_key: str | None = None,
        delay_s: float = 0,
        max_attempts: int | None = None,
    ) -> tuple[int, bool]:
        """Add a task. Returns (task_id, created). An existing idempotency key returns that task."""
        now = self.clock.now()
        attempts = max_attempts or self.cfg.for_type(task_type).max_attempts
        with tx(self.conn):
            if idempotency_key is not None:
                row = self.conn.execute(
                    "SELECT id FROM tasks WHERE idempotency_key = ?", (idempotency_key,)
                ).fetchone()
                if row:
                    return row["id"], False
            cur = self.conn.execute(
                "INSERT INTO tasks(type, payload_json, status, attempts, max_attempts, next_run_at, "
                "idempotency_key, created_at, updated_at) VALUES (?, ?, 'pending', 0, ?, ?, ?, ?, ?)",
                (
                    task_type,
                    json.dumps(payload or {}, ensure_ascii=False, sort_keys=True),
                    attempts,
                    iso(now + timedelta(seconds=delay_s)),
                    idempotency_key,
                    iso(now),
                    iso(now),
                ),
            )
            task_id = cur.lastrowid
            record_event(self.conn, self.clock, entity="task", entity_id=str(task_id), kind="created",
                         to_state="pending", data={"type": task_type})
        return task_id, True

    # --- consuming -------------------------------------------------------------------

    def reclaim_expired(self) -> int:
        """Return tasks whose lease expired (their runner died) to pending. No attempt is counted."""
        now = iso(self.clock.now())
        with tx(self.conn):
            cur = self.conn.execute(
                "UPDATE tasks SET status = 'pending', lease_owner = NULL, lease_until = NULL, updated_at = ? "
                "WHERE status = 'running' AND lease_until < ?",
                (now, now),
            )
        return cur.rowcount

    def lease(self, owner: str, types: list[str] | None = None) -> Task | None:
        """Atomically take the next due task, or None."""
        now = self.clock.now()
        until = iso(now + timedelta(seconds=self.cfg.lease_seconds))
        type_clause, params = "", []
        if types:
            type_clause = f" AND type IN ({', '.join('?' for _ in types)})"
            params = list(types)
        with tx(self.conn):
            row = self.conn.execute(
                "UPDATE tasks SET status = 'running', lease_owner = ?, lease_until = ?, updated_at = ? "
                "WHERE id = (SELECT id FROM tasks WHERE status = 'pending' AND next_run_at <= ?"
                f"{type_clause} ORDER BY next_run_at, id LIMIT 1) RETURNING *",
                (owner, until, iso(now), iso(now), *params),
            ).fetchone()
        return Task.from_row(row) if row else None

    def complete(self, task_id: int) -> None:
        now = iso(self.clock.now())
        with tx(self.conn):
            self.conn.execute(
                "UPDATE tasks SET status = 'done', lease_owner = NULL, lease_until = NULL, last_error = NULL, "
                "attempts = attempts + 1, updated_at = ? WHERE id = ? AND status = 'running'",
                (now, task_id),
            )
            record_event(self.conn, self.clock, entity="task", entity_id=str(task_id), kind="transition",
                         from_state="running", to_state="done")

    def release(self, task_id: int) -> None:
        """Give a leased task back untouched (e.g. the runner is stopping before it started)."""
        now = iso(self.clock.now())
        with tx(self.conn):
            self.conn.execute(
                "UPDATE tasks SET status = 'pending', lease_owner = NULL, lease_until = NULL, updated_at = ? "
                "WHERE id = ? AND status = 'running'",
                (now, task_id),
            )

    def fail(self, task_id: int, error: str, *, permanent: bool = False) -> FailOutcome:
        """Record a failed attempt: reschedule with backoff, or dead-letter."""
        now = self.clock.now()
        error = error[:2000]
        with tx(self.conn):
            row = self.conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            attempts = row["attempts"] + 1
            tcfg = self.cfg.for_type(row["type"])
            if permanent or attempts >= row["max_attempts"]:
                self.conn.execute(
                    "UPDATE tasks SET status = 'dead', attempts = ?, last_error = ?, lease_owner = NULL, "
                    "lease_until = NULL, updated_at = ? WHERE id = ?",
                    (attempts, error, iso(now), task_id),
                )
                record_event(self.conn, self.clock, entity="task", entity_id=str(task_id), kind="transition",
                             from_state="running", to_state="dead",
                             data={"type": row["type"], "attempts": attempts, "error": error[:300]})
                return FailOutcome(dead=True, retry_in_s=None)
            delay = backoff_delay(attempts, tcfg.backoff_base_s, tcfg.backoff_cap_s, self.rng)
            self.conn.execute(
                "UPDATE tasks SET status = 'pending', attempts = ?, last_error = ?, next_run_at = ?, "
                "lease_owner = NULL, lease_until = NULL, updated_at = ? WHERE id = ?",
                (attempts, error, iso(now + timedelta(seconds=delay)), iso(now), task_id),
            )
            record_event(self.conn, self.clock, entity="task", entity_id=str(task_id), kind="retry",
                         data={"attempt": attempts, "retry_in_s": round(delay, 1), "error": error[:300]})
        return FailOutcome(dead=False, retry_in_s=delay)

    # --- inspection ------------------------------------------------------------------

    def get(self, task_id: int) -> Task | None:
        row = self.conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return Task.from_row(row) if row else None

    def counts(self) -> dict[str, int]:
        rows = self.conn.execute("SELECT status, COUNT(*) AS n FROM tasks GROUP BY status").fetchall()
        return {r["status"]: r["n"] for r in rows}

    def due_count(self) -> int:
        now = iso(self.clock.now())
        return self.conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE status = 'pending' AND next_run_at <= ?", (now,)
        ).fetchone()[0]

    def dead(self, limit: int = 10) -> list[Task]:
        rows = self.conn.execute(
            "SELECT * FROM tasks WHERE status = 'dead' ORDER BY updated_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [Task.from_row(r) for r in rows]
