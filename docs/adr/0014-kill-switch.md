# 0014. Kill switch

Date: 2026-11-26 · Status: accepted

## Decision
* `/pause` sets `control.paused = 1`, then SIGTERMs every pid in `runs` with status `running` on
  this host, waits up to 8 s, and SIGKILLs survivors. Processes run as the same user, so no extra
  privileges are needed.
* Runners check the flag at start, before each task and before each side effect.
* Side effects write `side_effects(key, status='planned')` before acting and `done` after. A
  `planned` row found by a later run becomes `unknown` and is reported to the owner, never retried.
* The laptop worker receives the flag in every heartbeat (ADR 0006).
* `/resume` clears the flag. `DRY_RUN` is per platform and defaults to true.
