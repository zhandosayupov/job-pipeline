# Later: after the main sprints (S0–S6)

Ideas and decisions parked on purpose. Each item becomes an ADR or a sprint task when picked up.

## Further decisions

- **Salary expectation statistics.** Collect salary data per role, level and country (from the
  ranges published in postings we already collect, plus public salary sources) and build a
  reference table. With it, `policy.yaml: sensitive_answers.compensation` could move from `ask`
  to a market-based answer instead of asking the owner every time. Until then: always ask
  (ADR 0016), or `posting_range` for postings that publish a range.

## Sources not in S1

- **LinkedIn** (discovery only, ADR 0011): `python-jobspy` pulls in pandas, too heavy for the 1 GB VM.
  Options: run it on the laptop worker (S4), or a small own client for the public guest search.
- **relocate.me**: HTML listing pages (robots.txt allows them; `/api/` is disallowed).
- **Habr Career, getmatch, Remowork**: JS-heavy, need the browser worker (S4).
- **hh.kz**: saved-search email alerts parsed from Gmail (S5) and search pages in the worker (S4).

## Tailoring (after S3)

- **KB extraction helper**: `jobpipe kb extract cv_canonical.md` drafting `facts.yaml` with the LLM
  (pseudonymized) for the owner to review, instead of writing it by hand.
- **Owner's LaTeX templates**: adapt `cv_*_hft.tex` / `cv_*_generic.tex` to the placeholders of
  `templates/cv.tex` and pick the template by `resume_variant`.
- **tectonic on the VM**: the binary plus its bundle cache is several hundred MB; compile on the laptop
  worker (S4) or install it in `provision.sh` behind a flag.
- **Re-label with the full text** before tailoring (labels use a 700-character excerpt).

## Applying (after S4)

- **Mini App** for the approval queue (filter by `needs_human`, approve in a batch).
- **Answering a question from Telegram**: reply to a card with the answer and save it to
  `answers.yaml`, instead of editing the file.
- **LinkedIn Easy-Apply-only** postings: notification only (ADR 0011); **Workday**: not attempted.
- **Ashby comboboxes** and other custom widgets may need platform-specific fill code.
- **Live selector check**: a weekly dry run per platform that alerts when a selector stops matching.

## Inbox (after S5)

- **hh.kz chat** read in the browser worker (today: hh e-mail notifications are triaged, replies go on hh).
- **LinkedIn job-alert e-mails** as postings (company is plain text, not a link: needs a parser).
- **Answer a placeholder from Telegram**: reply to the card with the value instead of editing in Gmail.
- **Calendar slots** for interview invites instead of the `[[ВПИШИ: удобное время]]` placeholder.

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

## Doc nits

- `docs/adr/0018-retention.md`: add the cleanup of idle `provider_state` rows to the list of what
  retention deletes.
- `docs/architecture.md` (data model table): `provider_state` is per target (`provider/model`), not
  "per-provider" (ADR 0004 already says so).

## Tooling

- **Security review workflow** (`anthropics/claude-code-security-review`): needs an Anthropic API
  key (`CLAUDE_API_KEY`), not the subscription token. Worth adding before S4/S5, when code starts
  acting on the owner's behalf.
- **CI failure auto-fix workflow** (`ci-failure-auto-fix.yml` from the claude-code-action
  examples).
- **Stricter review for risky paths** (`pr-review-filtered-paths.yml`) on `adapters/appliers/` and
  `adapters/inbox/`.
