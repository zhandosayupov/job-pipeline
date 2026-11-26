"""Daily digest (09:00 Almaty): every job that passed the prefilter and was never sent before.

A job is marked `digested_at` only after Telegram accepted every message, so a failed send is
retried by the next digest and no job is ever sent twice.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from ..adapters.storage.db import tx
from ..clock import Clock, iso
from ..ports.notifier import Notifier
from . import control

LIVE_STATES = ("prefiltered", "labeled", "shortlisted")
LAST_DIGEST = "digest.last_at"
MAX_MESSAGE = 3800


@dataclass
class Digest:
    messages: list[str]
    job_ids: list[str]


def _priority(row: sqlite3.Row, sig: dict) -> int:
    score = 0
    score += {"explicit": 4, "relocation_only": 2}.get(sig.get("visa") or "", 0)
    score += 2 if sig.get("remote") == "remote" else 0
    score += 2 if (row["country"] or "") == "KZ" else 0
    score += 1 if row["kind"] == "ats" else 0
    score += 2 if "c++" in (row["title"] or "").lower() else 0
    return score


def _line(row: sqlite3.Row, sig: dict) -> str:
    marks = []
    visa = sig.get("visa")
    if visa == "explicit":
        marks.append("виза ✓")
    elif visa == "relocation_only":
        marks.append("релокация")
    if sig.get("remote") == "remote":
        marks.append("remote")
    if sig.get("salary"):
        marks.append(str(sig["salary"])[:40])
    where = row["location"] or row["country"] or ""
    head = " — ".join(x for x in (row["company"], row["title"]) if x)
    meta = " · ".join(x for x in (where[:50], *marks) if x)
    return f"• {head}" + (f"\n  {meta}" if meta else "") + f"\n  {row['url']}"


def build(conn: sqlite3.Connection, clock: Clock, tz: str, max_items: int) -> Digest:
    marks = ", ".join("?" for _ in LIVE_STATES)
    rows = conn.execute(
        f"SELECT id, company, title, url, location, country, kind, posted_at, data_json FROM jobs "
        f"WHERE state IN ({marks}) AND digested_at IS NULL", LIVE_STATES).fetchall()
    items = [(r, json.loads(r["data_json"] or "{}")) for r in rows]
    items.sort(key=lambda it: (_priority(*it), it[0]["posted_at"] or ""), reverse=True)

    since = control.get_flag(conn, LAST_DIGEST) or "0000"
    stats = conn.execute(
        "SELECT COALESCE(SUM(fetched),0) f, COALESCE(SUM(new),0) n, COALESCE(SUM(dropped),0) d "
        "FROM source_runs WHERE started_at >= ?", (since,)).fetchone()
    unhealthy = conn.execute(
        "SELECT s.source, s.status FROM source_runs s JOIN (SELECT source, MAX(id) id FROM source_runs GROUP BY source) "
        "m ON m.id = s.id WHERE s.status NOT IN ('ok', 'skipped') ORDER BY s.source").fetchall()

    day = clock.now().astimezone(ZoneInfo(tz)).strftime("%d.%m")
    header = (f"Дайджест {day}: {len(items)} новых вакансий после фильтра "
              f"(получено {stats['f']}, новых {stats['n']}, отсеяно {stats['d']}).")
    if unhealthy:
        header += "\nИсточники с проблемами: " + ", ".join(f"{r['source']} ({r['status']})" for r in unhealthy)
    lines = [_line(r, s) for r, s in items[:max_items]]
    if len(items) > max_items:
        lines.append(f"…и ещё {len(items) - max_items} с меньшим приоритетом (разметка в S2 отберёт лучшие).")
    if not items:
        lines.append("Новых вакансий нет.")

    messages, cur = [], header
    for line in lines:
        if len(cur) + len(line) + 2 > MAX_MESSAGE:
            messages.append(cur)
            cur = line
        else:
            cur += "\n\n" + line
    messages.append(cur)
    return Digest(messages, [r["id"] for r, _ in items])


def send(conn: sqlite3.Connection, clock: Clock, notifier: Notifier, tz: str, max_items: int) -> Digest | None:
    """Build, send, and mark as digested only if every message was delivered."""
    d = build(conn, clock, tz, max_items)
    for i, m in enumerate(d.messages):
        if not notifier.send(m, silent=i > 0):
            return None
    now = iso(clock.now())
    with tx(conn):
        conn.executemany("UPDATE jobs SET digested_at = ? WHERE id = ?", [(now, j) for j in d.job_ids])
    control.set_flag(conn, clock, LAST_DIGEST, now, actor="digest")
    return d
