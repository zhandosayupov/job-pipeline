# Later: after the main sprints (S0–S6)

Ideas and decisions parked on purpose. Each item becomes an ADR or a sprint task when picked up.

## Further decisions

- **Salary expectation statistics.** Collect salary data per role, level and country (from the
  ranges published in postings we already collect, plus public salary sources) and build a
  reference table. With it, `policy.yaml: sensitive_answers.compensation` could move from `ask`
  to a market-based answer instead of asking the owner every time. Until then: always ask
  (ADR 0016), or `posting_range` for postings that publish a range.

## Sources not in S1

- **Owner's list, checked on 2026-11-27, not collected** (the reason in brackets):
  * 403 to our requests, so we stop (ADR 0012): Citadel, Citadel Securities, Two Sigma, Astana Hub.
  * No vacancy pages with JobPosting in the sitemap: Booking.com, SIG, Spotify, Halyk Bank, Halyk Finance, Nebius careers (Nebius is collected through Greenhouse instead).
  * No sitemap, or the job pages are rendered by JavaScript: Apple, Microsoft, Netflix, ByteDance/TikTok, Mistral, D. E. Shaw, QRT, Wintermute, Auros.
  * Uber: the sitemap leads to rider and driver pages (HTTP 406).
  * Yandex: there is no jobs sitemap.
  * KZ banks and telcos (Kcell, Beeline/QazCode, ForteBank, Bereke, Sergek): they post through hh.kz, so turn on the `hh` source.
- **Career sites checked on 2026-11-27 and left out**:
  * Google: robots.txt disallows `/about/careers/applications/jobs/results`.
  * careers.indrive.com: vacancy pages came back empty (rendered by JavaScript), so they need the worker.
  * kolesa.group: no vacancy sitemap.
  * getmatch and relocate.me search pages are rendered by JavaScript (relocate.me is collected through its jobs sitemap instead).
  * EPAM: `/api` is disallowed; check its sitemap.
  * Workday tenants beyond Nvidia: find each company's host, tenant and site.

- **LinkedIn** (discovery only, ADR 0011): `python-jobspy` pulls in pandas, too heavy for the 1 GB VM.
  Options: run it on the laptop worker (S4), or a small own client for the public guest search.
- **relocate.me**: HTML listing pages (robots.txt allows them; `/api/` is disallowed).
- **Habr Career, getmatch, Remowork**: JS-heavy, need the browser worker (S4).
- **hh.kz**: saved-search email alerts parsed from Gmail (S5) and search pages in the worker (S4).

## Tailoring (after S3)

- ~~KB extraction helper~~: done in S7 (`jobpipe kb extract`).
- **Owner's LaTeX templates**: adapt `cv_*_hft.tex` / `cv_*_generic.tex` to the placeholders of
  `templates/cv.tex` and pick the template by `resume_variant`.
- **tectonic on the VM**: the binary plus its bundle cache is several hundred MB; compile on the laptop
  worker (S4) or install it in `provision.sh` behind a flag.
- **Open technology list in the truth gate**: also treat capitalised product-like words outside `TECH`
  as claims, or compare against the job's tech list (S3 review).
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
- ~~Answer a placeholder from Telegram~~, ~~calendar slots~~: done in S7 (ADR 0025).
- **Recurring calendar events (RRULE)**: only the first occurrence counts as busy today.
- **WhatsApp Web reader** on the laptop (read-only, no auto-send); ToS/ban risk, so opt-in only.
- **Save a filled placeholder to answers.yaml** (one tap: reuse the answer next time).

## Production

- ~~Production checks~~: done in S6 (`jobpipe selfcheck`, ADR 0024). Still open: a daily LLM ping per
  provider inside selfcheck.
- **`uv sync` runs as root during provisioning.** It builds the project from `pyproject.toml`,
  so a build backend from the cloned repo runs as root. Fine while the repo is ours; harden later
  by deploying a pinned commit or signed tag instead of a branch head, and note it in
  `docs/infra.md`.
- ~~Rollback copy during provisioning~~: done in S6 (the old tree stays until `uv sync` passes).

## Doc nits

- ~~ADR 0018 provider_state, architecture provider_state~~: done in S7.

## Tooling

- **Security review workflow** (`anthropics/claude-code-security-review`): needs an Anthropic API
  key (`CLAUDE_API_KEY`), not the subscription token. Worth adding before S4/S5, when code starts
  acting on the owner's behalf.
- **CI failure auto-fix workflow** (`ci-failure-auto-fix.yml` from the claude-code-action
  examples).
- **Stricter review for risky paths** (`pr-review-filtered-paths.yml`) on `adapters/appliers/` and
  `adapters/inbox/`.
