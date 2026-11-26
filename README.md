# jobpipe

A self-hosted job search pipeline for one person: collect vacancies, filter and label them with
LLMs, tailor a CV and cover letter from a fact base, apply through a browser, read replies, draft
answers, track the funnel. You drive it from a Telegram bot, and nothing goes out on your behalf
without your Approve.

Built to run on a free GCP e2-micro (1 GB RAM) plus a laptop for the browser.
Architecture: [docs/architecture.md](docs/architecture.md) · decisions: [docs/adr/](docs/adr/) ·
plan: [docs/plan.html](docs/plan.html) · parked for later: [docs/TODO.md](docs/TODO.md).

## Status

| sprint | scope | state |
|---|---|---|
| S0 | skeleton: config, SQLite + migrations, state machines, events, task queue, LLM router, bot (`/status` `/pause` `/resume`), systemd, provisioning, CI | in review |
| S1 | sources, prefilter, dedupe, daily digest | next |
| S2–S6 | labeling, tailoring, applying, inbox, hardening | planned |

## Ground rules (enforced in code)

* Every CV line, letter and answer comes from your fact base; unmatched claims block the item.
* Applications are submitted automatically only when every check passes (not a protected company,
  every question answered from your KB, truth gate passed, dry run off, daily cap left); otherwise
  you get an approval card. Messages to people always wait for your Approve on sensitive topics.
* One application per vacancy, ever. Every external side effect is journaled before it happens.
* Everything carries an attention tag (`ok` / `needs_human` / `not_ok`), so you see what needs you.
* Old detail is deleted or compacted to a resolution every night: the VM has 1 GB RAM.
* `/pause` stops everything within 10 seconds. `DRY_RUN` is on per platform until you turn it off.
* Captcha, 2FA and security checks go to you. No solvers, proxies or fingerprint tricks.
* Secrets and personal data never enter this repo (see below).

## Your data stays out of the repo

The repo holds code, prompts and `config.example/`. Everything personal lives in `JOBPIPE_HOME`
(default `~/.jobpipe`): config, the fact base, the database, and `leakguard.txt`, a list of private
strings the pre-commit hook refuses to commit.

```bash
git config core.hooksPath .githooks   # once per clone: enables the leak guard
```

## Local quick start

```bash
uv sync
uv run jobpipe init                     # creates ~/.jobpipe with example config
uv run jobpipe task add selftest.ok
uv run jobpipe run queue
uv run jobpipe status
```

With a Gemini key in `~/.jobpipe/secrets.env`: `uv run jobpipe llm ping`.
With a bot token and `telegram.owner_id`: `uv run jobpipe bot`, then `/status` in Telegram.

## Deploy

See [docs/infra.md](docs/infra.md). In short, on a fresh Debian 12 VM as root:

```bash
git clone https://github.com/zhandosayupov/job-pipeline.git /tmp/jp && bash /tmp/jp/ops/provision.sh
```

## Commands

| command | what |
|---|---|
| `jobpipe init` | create `JOBPIPE_HOME`, copy example config, migrate |
| `jobpipe bot` | resident bot + `/healthz` (systemd: `jobpipe-bot.service`) |
| `jobpipe run queue` | drain due tasks, exit (timer: every minute) |
| `jobpipe task add TYPE [--payload JSON] [--key K]` | enqueue; same key returns the existing task |
| `jobpipe task list [--status dead]` | inspect the queue |
| `jobpipe pause` / `resume` / `status` | same as the bot commands |
| `jobpipe llm ping` | one call through the LLM router |
| `jobpipe backup` | online SQLite backup (timer: 03:00 Almaty) |
| `jobpipe run retention [--dry-run]` | delete / compact old rows (timer: 04:00 Almaty) |
| `jobpipe effects list` / `effects resolve KEY done\|failed\|reset` | settle actions whose outcome is unknown |

## Development

```bash
uv run pytest -q
uv run ruff check .
uv run python tools/leak_guard.py --all
```

License: MIT.
