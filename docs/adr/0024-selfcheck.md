# 0024. Self-check, dead-man's switch, restore test, earned L2 for replies

Date: 2026-11-27 · Status: accepted (S6)

## Decision
* **`jobpipe selfcheck`, hourly** (`jobpipe-selfcheck.timer`). It checks:
  * the database is writable and its schema is current;
  * no task has been overdue for more than 30 minutes, no task is dead, no side effect is `unknown`;
  * every timer ran recently: each successful `jobpipe run <x>` stamps `last_run.<x>` (without an event row), with limits per command in `settings.yaml: selfcheck.max_age_hours`;
  * last night's backup exists and passes `quick_check`;
  * every worker sent a heartbeat within 24 h;
  * every enabled source fetched within 30 h.

  A failing check alerts **once**, and its recovery is reported once (flags `selfcheck.<name>`).
* **Dead-man's switch.** When every check passes, `selfcheck.ping_url` is pinged. Any free "cron monitor" service works (healthchecks.io, for example). It alerts the owner when the pings stop, which covers a VM that is down or unreachable and cannot alert by itself.
* **Restore test.** `jobpipe backup --verify` restores the latest backup into a scratch file with the backup API. It runs `integrity_check` and the migrations on the copy, prints the row counts and deletes the copy.
* **L2 for replies is earned.** Even when `policy.yaml` sets `reply: L2`, a draft goes to Gmail on its own only after `l2_min_clean_approvals` drafts were sent unedited. That count comes from the `reply_outcome` events recorded in S5. Until then the reply stays a card. Sensitive topics are never automatic.
* **The digest** gets one funnel line covering the period since the previous digest: applied, replied, screening, interview, offer, rejected, ghosted, plus what is waiting for the owner.
* **Provisioning rollback.** The previous code tree is kept until `uv sync` of the new tree succeeds and the new entry point starts. If either fails, the old tree comes back and its units are started again.
