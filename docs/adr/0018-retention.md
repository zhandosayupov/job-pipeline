# 0018. Retention: delete old detail, keep resolutions

Date: 2026-11-26 · Status: accepted

## Context
The VM has 1 GB RAM and a 30 GB disk, and SQLite pages compete with everything else for page
cache. Most rows matter for days (tasks, runs, raw LLM calls, job descriptions); what matters
later is only enough to recognise the same thing again (dedupe, one application per vacancy) and
to count the funnel.

## Decision
`jobpipe run retention`, daily at 04:00 Almaty (after the 03:00 backup), in one transaction;
`--dry-run` shows the counts and rolls back. Defaults in `settings.yaml → retention`:

| what | after | becomes |
|---|---|---|
| done tasks / dead tasks | 7 / 30 days | deleted |
| finished runs | 14 days | deleted |
| `llm_calls` | 35 days | summed into `llm_daily` (day, provider, task), then deleted |
| events of jobs, tasks, control | 60 days | deleted; application, conversation and side-effect events are kept (funnel history) |
| finished jobs (`dropped`, `skipped`, `closed`) | 30 days | row kept, `data_json` reduced to `keep_fields` + `resolution` |
| finished applications (terminal states) | 180 days | same |
| done/failed side effects | 90 days | key kept forever (idempotency), payload dropped |

* Rows that need the owner (`needs_human`, unknown side effects) are never compacted.
* New databases use `auto_vacuum=INCREMENTAL`; retention ends with `PRAGMA incremental_vacuum`
  and a WAL checkpoint, so space comes back without a full VACUUM (which needs 2x disk).
* Artifacts on disk (CV PDFs, screenshots) get the same treatment when they appear in S3/S4.

## Consequences
Old job descriptions cannot be re-read; re-labelling a months-old job means fetching it again.
The append-only rule for `events` now has one exception: retention is the only deleter.
