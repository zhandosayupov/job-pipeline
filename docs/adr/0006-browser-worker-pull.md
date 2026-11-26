# 0006. Browser worker and userbot on the owner's laptop, pulling tasks

Date: 2026-11-26 · Status: accepted (implemented in S4 and S5)

## Context
Logging in to hh.kz, LinkedIn or Telegram from a US datacenter IP triggers security checks, and
Chromium needs 400–700 MB, which the VM lacks. The laptop has a residential KZ IP but sits behind NAT.

## Decision
* A small `worker-agent` runs on the laptop, started by Windows Task Scheduler at logon, hidden,
  restarted on failure, process priority BELOW_NORMAL.
* It long-polls the core (`GET /worker/lease`, 25 s) over Tailscale with a bearer token, executes
  the task, posts the result and artifacts back. Leases have heartbeats; the pause flag comes back
  in every heartbeat and is checked immediately before a submit click.
* Chromium starts per batch with a dedicated persistent profile per platform and closes after it.
  Applying runs only on AC power and after N minutes without user input (configurable).
* The Telethon userbot runs inside the same process.
* The core tracks worker heartbeats; `/status` shows the worker, and the bot alerts when it is
  offline during an apply window.
* Where the browser runs (`laptop` or `vm`) is a config value.

## Consequences
Applications wait while the laptop is off. Tasks carry a TTL; expired ones go back to the owner.
