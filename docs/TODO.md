# Later: after the main sprints (S0–S6)

Ideas and decisions parked on purpose. Each item becomes an ADR or a sprint task when picked up.

## Further decisions

- **Salary expectation statistics.** Collect salary data per role, level and country (from the
  ranges published in postings we already collect, plus public salary sources) and build a
  reference table. With it, `policy.yaml: sensitive_answers.compensation` could move from `ask`
  to a market-based answer instead of asking the owner every time. Until then: always ask
  (ADR 0016), or `posting_range` for postings that publish a range.

## Production

- **Production checks that alert when something breaks.** Unit tests only cover the code; on the
  VM we also need scheduled checks with a Telegram alert on failure, for example an hourly
  `jobpipe selfcheck`: database writable and migrations current, queue moving (no task overdue
  for long), every timer ran on schedule, backup from last night exists and passes
  `integrity_check`, a daily LLM ping per enabled provider, each source fetched recently, the
  laptop worker's last heartbeat. Plus an external dead-man's switch (an outside service that
  alerts if the VM stops checking in), because a dead VM cannot send its own alert.
- **`uv sync` runs as root during provisioning.** It builds the project from `pyproject.toml`,
  so a build backend from the cloned repo runs as root. Fine while the repo is ours; harden later
  by deploying a pinned commit or signed tag instead of a branch head, and note it in
  `docs/infra.md`.
- **Rollback copy during provisioning.** `provision.sh` deletes `/opt/jobpipe.old` right after the
  swap, before `uv sync` of the new tree has passed, so a failed sync leaves nothing to roll back
  to. Keep the old tree until the sync (and ideally a `/healthz` check) succeeds, and restore it
  plus restart the units if they fail.

## Tooling

- **Security review workflow** (`anthropics/claude-code-security-review`): needs an Anthropic API
  key (`CLAUDE_API_KEY`), not the subscription token. Worth adding before S4/S5, when code starts
  acting on the owner's behalf.
- **CI failure auto-fix workflow** (`ci-failure-auto-fix.yml` from the claude-code-action
  examples).
- **Stricter review for risky paths** (`pr-review-filtered-paths.yml`) on `adapters/appliers/` and
  `adapters/inbox/`.
