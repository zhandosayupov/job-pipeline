# 0003. Task queue in SQLite

Date: 2026-11-26 · Status: accepted

## Decision
* `tasks(id, type, payload_json, status, attempts, max_attempts, next_run_at, idempotency_key UNIQUE,
  last_error, lease_owner, lease_until, created_at, updated_at)`.
* Status: `pending → running → done`, or back to `pending` with a later `next_run_at`, or `dead`.
* Lease: one atomic `UPDATE ... WHERE id = (SELECT ... LIMIT 1) RETURNING *`. Expired leases are
  reclaimed to `pending` without counting an attempt, but `reclaims` is counted: a task whose
  runner dies `max_reclaims` times (OOM, systemd timeout) is dead-lettered with an alert instead
  of looping forever. `lease_seconds` must exceed the runner's hard timeout (validated in config).
* Backoff: equal jitter, `d = min(cap, base * 2^(attempt-1))`, delay = `d/2 + rand(0, d/2)`.
  Base, cap and max attempts are configurable per task type.
* Dead-letter: after `max_attempts` the task is `dead` and the owner gets a Telegram alert.
* Enqueue with an existing idempotency key returns the existing task.
* A handler may raise `Permanent` to dead-letter at once (a validation error that retrying will not fix).

## Consequences
Handlers run at least once. Handlers with external side effects must use the `side_effects`
table (ADR 0014) to be at most once.
