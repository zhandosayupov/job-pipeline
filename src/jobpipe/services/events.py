"""Append-only event log and checked state transitions."""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from ..adapters.storage.db import tx
from ..clock import Clock, iso
from ..domain import states
from ..domain.errors import StaleState

_TABLES = {"job": "jobs", "application": "applications", "conversation": "conversations"}


def record_event(
    conn: sqlite3.Connection,
    clock: Clock,
    *,
    entity: str,
    entity_id: str,
    kind: str,
    actor: str = "system",
    from_state: str | None = None,
    to_state: str | None = None,
    data: dict[str, Any] | None = None,
) -> int:
    """Insert one event. Call inside the caller's transaction when it accompanies a change."""
    cur = conn.execute(
        "INSERT INTO events(ts, entity, entity_id, kind, from_state, to_state, actor, data_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            iso(clock.now()),
            entity,
            entity_id,
            kind,
            from_state,
            to_state,
            actor,
            json.dumps(data or {}, ensure_ascii=False, sort_keys=True),
        ),
    )
    return cur.lastrowid


def create_entity(
    conn: sqlite3.Connection,
    clock: Clock,
    entity: str,
    entity_id: str,
    *,
    actor: str = "system",
    columns: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
) -> None:
    """Insert a job/application/conversation in its initial state plus a `created` event."""
    table = _TABLES[entity]
    _, initial = states.MACHINES[entity]
    now = iso(clock.now())
    cols = {"id": entity_id, "state": initial, "created_at": now, "updated_at": now, **(columns or {})}
    if data is not None:
        cols["data_json"] = json.dumps(data, ensure_ascii=False, sort_keys=True)
    names = ", ".join(cols)
    marks = ", ".join("?" for _ in cols)
    with tx(conn):
        conn.execute(f"INSERT INTO {table}({names}) VALUES ({marks})", tuple(cols.values()))
        record_event(conn, clock, entity=entity, entity_id=entity_id, kind="created", actor=actor,
                     to_state=initial)


def transition(
    conn: sqlite3.Connection,
    clock: Clock,
    entity: str,
    entity_id: str,
    to_state: str,
    *,
    expected: str | None = None,
    actor: str = "system",
    data: dict[str, Any] | None = None,
) -> str:
    """Move an entity to `to_state` and log it, atomically. Returns the previous state.

    Raises InvalidTransition if the machine forbids it, StaleState if the row changed underneath
    (or is not in `expected`), KeyError if the entity does not exist.
    """
    table = _TABLES[entity]
    with tx(conn):
        row = conn.execute(f"SELECT state FROM {table} WHERE id = ?", (entity_id,)).fetchone()
        if row is None:
            raise KeyError(f"{entity} {entity_id} not found")
        current = row["state"]
        if expected is not None and current != expected:
            raise StaleState(f"{entity} {entity_id} is {current}, expected {expected}")
        states.check_transition(entity, current, to_state)
        cur = conn.execute(
            f"UPDATE {table} SET state = ?, updated_at = ? WHERE id = ? AND state = ?",
            (to_state, iso(clock.now()), entity_id, current),
        )
        if cur.rowcount != 1:
            raise StaleState(f"{entity} {entity_id} changed concurrently")
        record_event(conn, clock, entity=entity, entity_id=entity_id, kind="transition", actor=actor,
                     from_state=current, to_state=to_state, data=data)
    return current
