# 0017. Attention tags on states

Date: 2026-11-26 · Status: accepted

## Context
The owner wants to see at a glance what needs them, in `/status` now and in the Mini App later,
without memorising a few dozen state names.

## Decision
Every state of every machine, plus task and side-effect statuses, carries one tag in
`domain/states.ATTENTION`:

| tag | meaning | examples |
|---|---|---|
| `ok` | moving along, nothing to do | discovered, filling, applied, waiting |
| `needs_human` | waiting for the owner | awaiting_approval, apply_failed, submit_unknown, interview, offer, draft_ready, dead task, unknown side effect |
| `not_ok` | ended badly or filtered out; already handled, nothing to do | dropped, skipped, closed, rejected, ghosted, withdrawn, failed side effect |

* A protected job on the shortlist is `needs_human` (it is never auto-applied).
* Tags are derived from the state, not stored: `states_tagged(entity, tag)` gives the state list
  for SQL filters, so changing a tag needs no migration.
* A test asserts every state has exactly one tag and no terminal state is `ok`.

## Consequences
`/status` shows a "waiting for you" line with counts per kind. The Mini App (S4) filters on the
same tags. Retention (ADR 0018) compacts only `not_ok` and terminal rows.
