# 0014. Kill switch

Date: 2026-11-26 · Status: accepted

## Decision
* `/pause` sets `control.paused = 1`, then SIGTERMs every process recorded as running in `runs` on
  this host, waits up to 8 s, and SIGKILLs survivors. Processes run as the same user, so no extra
  privileges are needed.
* `runs.proc_start` stores a process identity (boot id + start time on Linux, creation time on
  Windows). A pid whose identity no longer matches was reused by another process: it is never
  signalled, and its run is marked failed.
* The runner's SIGTERM handler only sets a flag (raising from a signal handler could leave a
  transaction open). Handlers call `ctx.guard()` before side effects and inside long loops; it
  raises on the flag or the pause switch, and the task is released untouched. A handler stuck in
  one long call is covered by the SIGKILL. The handler is installed before the run is recorded.
* Runners check the flag at start, before each task and before each side effect.
* Side effects write `side_effects(key, status='planned')` before acting and `done` after. A
  `planned` row whose run is gone becomes `unknown`: the owner is alerted (retried every run until
  the alert is delivered, tracked by `notified_at`), and `/status` lists every unknown key. It is
  never retried automatically.
* The owner resolves a key with `jobpipe effects resolve KEY done|failed|reset`: `done` it
  happened, `failed` it did not and stays blocked, `reset` deletes the key so the action may run
  again. Each resolution is an event.
* The laptop worker receives the flag in every heartbeat (ADR 0006).
* `/resume` clears the flag. `DRY_RUN` is per platform and defaults to true.
