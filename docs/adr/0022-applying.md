# 0022. Applying: worker API, form resolution on the VM, two-phase submit

Date: 2026-11-27 · Status: accepted (S4) · Implements ADR 0006, 0007, 0016

## Decision
* **Split of responsibilities.**
  * The laptop worker scans the form, fills what it is told, screenshots and clicks.
  * The VM decides everything else: the answers, the policy route, the moment of submit.
  * The worker holds no knowledge base and no policy.
* **Worker API** (`api/worker.py`, mounted in the resident bot process when `WORKER_TOKEN` is set):
  * endpoints: heartbeat (returns the pause flag), long-poll lease (≤ 25 s), resolve, artifact download, result upload (multipart with a screenshot);
  * bearer token compared in constant time, and the API is reached only over Tailscale (`http.host` = the VM's Tailscale address).
* **Form resolution** (`services/forms.py`) — nothing generated:
  * profile fields come from `profile.yaml`; the resume from the tailored CV; the cover letter from the approved letter;
  * sensitive topics follow `policy.yaml: sensitive_answers`: visa and work authorization are derived from the job's country, salary asks the owner by default;
  * everything else is a verbatim `answers.yaml` entry;
  * select, radio and checkbox values are mapped onto the form's own options;
  * unknown means unanswered, and an unanswered required field means a card.
* **Manifest hash** covers the answered fields **and** the unanswered ones, so a new question changes the hash (a bug found while testing: an added required field left the hash unchanged).
* **Flow** (`services/apply.py`):
  1. **Schedule.** `ready` applications with an approved CV (`require_cv_approval`) on supported platforms (`apply_platforms`) go to `filling`, and an `apply.fill` task is queued. Unsupported platforms get one "apply manually" note.
  2. **Fill.** The fill result goes through `decide_apply()`:
     * **submit:** the side-effect key is reserved, the application moves to `submitting`, and an `apply.submit` task is queued with the approved manifest hash;
     * **ask:** `awaiting_approval`, with a card (screenshot, what was filled, why it needs the owner, unanswered questions) and the buttons 🚀 Отправить / ✋ Не отправлять;
     * **dry run** (the default per platform): `awaiting_approval` with a report and 👌 Понятно, which moves it back to `ready`;
     * **defer** (daily cap or pause): `ready` again 12 hours later;
     * **block** (already applied, truth gate): `withdrawn`.
  3. **Submit.** The worker refills and resolves again. It never clicks when the hash differs or a required field is unanswered: the application goes back to the owner (`awaiting_approval`) and the key is freed. It checks the pause flag right before the click. Outcomes:
     * click and confirmation: `applied`, key `done`;
     * click without confirmation: `submit_unknown`, key `unknown`, the owner resolves it;
     * no click: `apply_failed`, key freed.
* **Captcha or a bot check.** The owner is alerted once per URL; the worker waits up to 10 minutes with the window open for the owner to solve it, then fails the task. No solvers, proxies or fingerprint changes (ADR 0012).
* **Appliers** (`worker/appliers.py`) are one generic flow configured per platform in `appliers.yaml`: URL suffix, clicks that reveal the form (hh's "Откликнуться"), submit selector, confirmation patterns and selectors. A missing selector is a failure with a screenshot, never a blind retry.
* **Laptop.**
  * Playwright Chromium with a dedicated persistent profile per platform, logged in once with `jobpipe worker --login hh`.
  * BELOW_NORMAL priority; applies only on AC power and after `min_idle_s` without input.
  * Started at logon by Windows Task Scheduler (`ops/worker/install-worker.ps1`).
  * Playwright is an optional dependency group (`worker`), so the VM never installs it.
* **Timer.** `jobpipe run apply` every 20 minutes, weekdays 10–19 Almaty, with jitter.

## Consequences
* Selectors in `appliers.yaml` are written from public markup, not verified against live forms; every
  platform starts in dry run, and the S4 done-condition requires 10 correct dry runs per platform.
* Ashby forms are rendered by JavaScript; the scanner reads the rendered DOM through Playwright, but
  custom widgets (comboboxes) may need platform-specific handling. Workday is not attempted.
