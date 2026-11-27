"""Retention (ADR 0018): the VM disk and RAM are small, so old detail is deleted or compacted to a
resolution, keeping only what future runs need to recognise things (dedupe, idempotency, funnel).

| what                          | after                       | becomes                                   |
|-------------------------------|-----------------------------|-------------------------------------------|
| done tasks                    | tasks_done_days             | deleted                                   |
| dead tasks                    | tasks_dead_days             | deleted                                   |
| finished runs                 | runs_days                   | deleted                                   |
| llm_calls                     | llm_calls_days              | summed into llm_daily, then deleted       |
| job/task/control/... events   | events_days                 | deleted (application/conversation kept)   |
| finished jobs                 | jobs_compact_days           | row kept, data_json -> keep_fields only   |
| finished applications         | applications_compact_days   | row kept, data_json -> keep_fields only   |
| done/failed side effects      | side_effects_compact_days   | key kept forever, data_json dropped       |

Unknown side effects and jobs/applications in needs_human states are never touched. Dead tasks
are deleted after tasks_dead_days: by then they were alerted and shown in /status for a month.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import timedelta

from ..adapters.storage.db import tx
from ..clock import Clock, iso
from ..config.settings import RetentionCfg
from ..domain import states

KEPT_EVENT_ENTITIES = ("application", "conversation", "side_effect")


class _DryRun(Exception):
    pass


def _compact(data_json: str, keep: list[str], resolution: str) -> str:
    try:
        data = json.loads(data_json)
    except ValueError:
        data = {}
    out = {k: data[k] for k in keep if k in data}
    out["resolution"] = resolution
    out["compacted"] = True
    return json.dumps(out, ensure_ascii=False, sort_keys=True)


def run_retention(
    conn: sqlite3.Connection, clock: Clock, cfg: RetentionCfg, *, dry_run: bool = False
) -> dict[str, int]:
    """Apply the policy in one transaction. With dry_run the counts are computed and rolled back."""
    now = clock.now()

    def cutoff(days: int) -> str:
        return iso(now - timedelta(days=days))

    counts: dict[str, int] = {}
    try:
        with tx(conn):
            counts["tasks_done"] = conn.execute(
                "DELETE FROM tasks WHERE status = 'done' AND updated_at < ?", (cutoff(cfg.tasks_done_days),)
            ).rowcount
            counts["tasks_dead"] = conn.execute(
                "DELETE FROM tasks WHERE status = 'dead' AND updated_at < ?", (cutoff(cfg.tasks_dead_days),)
            ).rowcount
            counts["runs"] = conn.execute(
                "DELETE FROM runs WHERE status != 'running' AND started_at < ?", (cutoff(cfg.runs_days),)
            ).rowcount

            llm_cut = cutoff(cfg.llm_calls_days)
            conn.execute(
                "INSERT INTO llm_daily(day, provider, task, calls, ok, tokens_in, tokens_out, cost_usd) "
                "SELECT substr(ts, 1, 10), provider, task, COUNT(*), SUM(status = 'ok'), SUM(tokens_in), "
                "SUM(tokens_out), SUM(cost_usd) FROM llm_calls WHERE ts < ? GROUP BY 1, 2, 3 "
                "ON CONFLICT(day, provider, task) DO UPDATE SET calls = calls + excluded.calls, "
                "ok = ok + excluded.ok, tokens_in = tokens_in + excluded.tokens_in, "
                "tokens_out = tokens_out + excluded.tokens_out, cost_usd = cost_usd + excluded.cost_usd",
                (llm_cut,),
            )
            counts["llm_calls"] = conn.execute("DELETE FROM llm_calls WHERE ts < ?", (llm_cut,)).rowcount

            marks = ", ".join("?" for _ in KEPT_EVENT_ENTITIES)
            counts["events"] = conn.execute(
                f"DELETE FROM events WHERE ts < ? AND entity NOT IN ({marks})",
                (cutoff(cfg.events_days), *KEPT_EVENT_ENTITIES),
            ).rowcount

            counts["jobs_compacted"] = _compact_table(
                conn, "jobs", states.states_tagged("job", states.Attention.NOT_OK),
                cutoff(cfg.jobs_compact_days), cfg.keep_fields.get("job", []))
            counts["applications_compacted"] = _compact_table(
                conn, "applications", frozenset(states.terminal_states(states.APPLICATION)),
                cutoff(cfg.applications_compact_days), cfg.keep_fields.get("application", []))

            # Rows with nothing active (no running cooldown or breaker, no failure streak) carry no
            # information; this also removes keys left over when a provider gained a models list.
            now_iso = iso(now)
            counts["provider_state"] = conn.execute(
                "DELETE FROM provider_state WHERE consecutive_failures = 0 "
                "AND (cooldown_until IS NULL OR cooldown_until < ?) AND (breaker_until IS NULL OR breaker_until < ?)",
                (now_iso, now_iso),
            ).rowcount

            counts["side_effects_compacted"] = conn.execute(
                "UPDATE side_effects SET data_json = '{}' WHERE status IN ('done', 'failed') "
                "AND updated_at < ? AND data_json != '{}'",
                (cutoff(cfg.side_effects_compact_days),),
            ).rowcount
            # S5: recruiter mail keeps its headers, category and triage, but the body and links go
            # once the conversation is idle this long (the funnel needs the category, not the text).
            counts["messages_compacted"] = conn.execute(
                "UPDATE messages SET body = '', data_json = json_remove(data_json, '$.links') "
                "WHERE ts < ? AND body != '' AND processed_at IS NOT NULL",
                (cutoff(cfg.messages_compact_days),),
            ).rowcount
            if dry_run:
                raise _DryRun()
    except _DryRun:
        return counts
    conn.execute("PRAGMA incremental_vacuum").fetchall()  # stepping to the end frees every free page
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
    return counts


def _compact_table(conn: sqlite3.Connection, table: str, final_states: frozenset[str], before: str,
                   keep: list[str]) -> int:
    marks = ", ".join("?" for _ in final_states)
    rows = conn.execute(
        f"SELECT id, state, data_json FROM {table} WHERE state IN ({marks}) AND updated_at < ? "
        "AND json_extract(data_json, '$.compacted') IS NOT 1",
        (*sorted(final_states), before),
    ).fetchall()
    for r in rows:
        compacted = _compact(r["data_json"], keep, r["state"])
        conn.execute(f"UPDATE {table} SET data_json = ? WHERE id = ?", (compacted, r["id"]))
    return len(rows)
