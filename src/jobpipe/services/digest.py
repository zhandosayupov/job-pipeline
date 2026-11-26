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
from . import text as T

LIVE_STATES = ("prefiltered", "labeled", "shortlisted")
LAST_DIGEST = "digest.last_at"
MAX_MESSAGE = 3800


@dataclass
class Digest:
    messages: list[str]
    job_ids: list[str]


def _priority(row: sqlite3.Row, sig: dict) -> int:
    """Labeled jobs (S2) rank by their LLM score, above every unlabeled one; unlabeled ones by signals."""
    label = sig.get("label") or {}
    if label.get("score") is not None:
        return 1000 + int(label["score"])
    score = 0
    score += {"explicit": 4, "relocation_only": 2}.get(sig.get("visa") or "", 0)
    score += 2 if sig.get("remote") == "remote" else 0
    score += 2 if (row["country"] or "") == "KZ" else 0
    score += 1 if row["kind"] == "ats" else 0
    score += 2 if "c++" in (row["title"] or "").lower() else 0
    return score


def _line(row: sqlite3.Row, sig: dict, also: list[str] | None = None) -> str:
    marks = []
    label = sig.get("label") or {}
    if label.get("score") is not None:
        marks.append(f"{label['score']}/100 {label.get('track', '')}".strip())
    visa = label.get("visa") if label.get("visa") not in (None, "unknown") else sig.get("visa")
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
    extra = ""
    if also:
        shown = ", ".join(x[:30] for x in also[:6])
        extra = f"\n  ещё {len(also)}: {shown}" + ("…" if len(also) > 6 else "")
    return f"• {head}" + (f"\n  {meta}" if meta else "") + extra + f"\n  {row['url']}"


def _group(items: list) -> list:
    """One line per (company, title): the same role posted in several countries is listed once,
    with the other locations appended. Order follows the best-ranked copy."""
    groups: dict[tuple[str, str], list] = {}
    order: list[tuple[str, str]] = []
    for r, s in items:
        key = (T.norm_company(r["company"]) or r["id"], " ".join((r["title"] or "").lower().split()))
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append((r, s))
    out = []
    for key in order:
        first, *others = groups[key]
        out.append((first, [o[0]["location"] or o[0]["country"] or "?" for o in others]))
    return out


def build(conn: sqlite3.Connection, clock: Clock, tz: str, max_items: int) -> Digest:
    marks = ", ".join("?" for _ in LIVE_STATES)
    rows = conn.execute(
        f"SELECT id, company, title, url, location, country, kind, posted_at, data_json FROM jobs "
        f"WHERE state IN ({marks}) AND digested_at IS NULL",
        LIVE_STATES,
    ).fetchall()
    items = [(r, json.loads(r["data_json"] or "{}")) for r in rows]
    items.sort(key=lambda it: (_priority(*it), it[0]["posted_at"] or ""), reverse=True)

    since = control.get_flag(conn, LAST_DIGEST) or "0000"
    stats = conn.execute(
        "SELECT COALESCE(SUM(fetched),0) f, COALESCE(SUM(new),0) n, COALESCE(SUM(dropped),0) d "
        "FROM source_runs WHERE started_at >= ?",
        (since,),
    ).fetchone()
    unhealthy = conn.execute(
        "SELECT s.source, s.status FROM source_runs s "
        "JOIN (SELECT source, MAX(id) id FROM source_runs GROUP BY source) "
        "m ON m.id = s.id WHERE s.status NOT IN ('ok', 'skipped') ORDER BY s.source"
    ).fetchall()

    day = clock.now().astimezone(ZoneInfo(tz)).strftime("%d.%m")
    header = (
        f"Дайджест {day}: {len(items)} новых вакансий после фильтра "
        f"(получено {stats['f']}, новых {stats['n']}, отсеяно {stats['d']})."
    )
    if unhealthy:
        header += "\nИсточники с проблемами: " + ", ".join(f"{r['source']} ({r['status']})" for r in unhealthy)
    groups = _group(items)
    lines = [_line(r, s, also) for (r, s), also in groups[:max_items]]
    if len(groups) > max_items:
        rest = sum(1 + len(also) for _, also in groups[max_items:])
        lines.append(f"…и ещё {rest} с меньшим приоритетом (разметка в S2 отберёт лучшие).")
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
