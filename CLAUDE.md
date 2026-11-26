# jobpipe: notes for Claude

Self-hosted job search pipeline for one owner. Read `docs/architecture.md` and the ADRs in
`docs/adr/` before changing a boundary; a boundary change adds a new ADR.

## Commands
- `uv sync` · `uv run pytest -q` · `uv run ruff check .` · `uv run python tools/leak_guard.py --all`
- CI (`ci.yml`) runs on code paths only; `leak-guard.yml` runs on every change

## How to review a PR here
- Review the PR's **current head**. Run `uv sync`, `uv run pytest -q`, `uv run ruff check .` and
  `uv run python tools/leak_guard.py --all` yourself and report the results; also read the PR's
  CI status. Do not ask the author to confirm CI.
- **Re-check every earlier finding against the current tree before repeating it**, code and docs
  alike (`docs/adr/`, `docs/architecture.md`, `docs/TODO.md`, `CLAUDE.md`). If a finding asks for
  documentation, grep the docs first. Report each earlier finding as fixed / still open with the
  file and line that proves it; never carry one over from a previous comment unverified.
- Items parked on purpose are listed in `docs/TODO.md`: do not report them as missing.
- List what you did not read, so the owner knows what the review does not cover.

## Rules that reviews must enforce
- **No secrets or personal data in the repo.** Code, prompts, synthetic fixtures and
  `config.example/` only. Personal data lives in `JOBPIPE_HOME`. Flag any real name, email, phone,
  CV fact or key-shaped string.
- **Truth gate.** Anything sent on the owner's behalf (CV, letter, form answer, message) is built
  only from facts in the KB. Generated text must never add facts.
- **Autonomy gate (ADR 0016).** Applications are auto-submitted (L2) only through
  `services.policy.decide_apply()`, when every check passes; anything else becomes an approval
  card. Messages default to L1. Protected companies are never automatic. Sensitive topics
  (compensation, notice, visa, start date, references, interviews, offers) are never answered
  automatically with generated text: in a message they always need Approve; in an application
  form each topic follows `policy.yaml: sensitive_answers` (salary asks the owner by default;
  visa/citizenship are derived from `work_authorized_countries` and the job's country; others
  use the owner's verbatim entry from `kb/answers.yaml`). Unknown inputs mean a card, never a guess.
- **Side effects are at most once.** Every external action reserves a key in `side_effects` before
  acting and checks the pause flag (`ctx.guard()`). No blind retries of submits or sends.
- **No bypassing defences.** Captcha, 2FA, 403/429 or robots refusals stop the platform and notify
  the owner. No captcha solvers, proxy rotation or fingerprint spoofing.
- **Unknown stays null.** Never infer a posting's salary, visa sponsorship or remote eligibility
  (e.g. from company reputation). The owner's own visa answer is derived from config (ADR 0016).
- **State changes** go through `services.events.transition()` so every change has an event row.
- **Tests**: every pure module has unit tests; HTTP goes through `httpx.MockTransport` with
  recorded fixtures; no network in the default test run.
- Target: Python 3.12, 1 GB RAM VM. Avoid heavy dependencies and long-lived processes.
