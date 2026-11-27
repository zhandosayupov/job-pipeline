# 0020. Labeling, shortlist, protected pool, cards

Date: 2026-11-27 · Status: accepted (S2)

## Context
S1 lets about a thousand postings a day through the prefilter. The owner needs a ranked shortlist,
cards to act on, and a guarantee that target firms (the protected pool) are never touched automatically.

## Decision
* **Labeling** (`services/label.py`, `jobpipe run label`, 20 minutes after each collect):
  * jobs in `prefiltered` go to the LLM router (task `label`, prompt `prompts/label/v1.md`) in batches
    of 20 (each well under 100k tokens), most promising first (protected, visa signal, remote, C++,
    ATS), at most `max_jobs_per_run` per run;
  * the prompt carries the spec §7 rubric, the owner's profile (`settings.yaml: label.profile`, private)
    and the visa-free countries from `policy.yaml`. Jobs get short ids (`j1..j20`) that are mapped back,
    so the model cannot invent ids; unknown or missing ids leave the job `prefiltered` for the next run;
  * output is not trusted as is: breakdown values are clamped to the rubric, freshness (0–10) is
    computed from `posted_at` here, and visa/remote evidence must be a quote that really occurs in the
    posting, else the field becomes `unknown` and is listed in `unverified`;
  * budget guard: at most `daily_max_jobs` labels per Almaty day; then labeling stops and the owner is
    told once that day. Provider quotas are still enforced by the router (ADR 0004).
* **Shortlist**: `score >= shortlist_threshold` and `action != skip` → `shortlisted`, else `skipped`.
  A protected job is always shortlisted (`needs_human`, ADR 0017): it goes to the owner, never further
  on its own.
* **Protected pool** (`protected.yaml`, `services/protected.py`): matching by normalised name or
  alias, URL domain (and subdomains), or ATS board token; entries past `until` are ignored. The
  collector marks matches and sends an instant alert. `/protect` and `/unprotect` edit the YAML
  atomically, re-mark live jobs and log an event. The example file is seeded with the spec's
  Tier 1-2 targets.
* **Cards**: an instant Telegram card with buttons for each protected or `score >= alert_threshold`
  shortlisted job (once per job): score breakdown, track, visa/remote with the verified quote, reason,
  link. Buttons: ✅ В работу (creates an application `draft`, idempotency key
  `apply:<company>:<job id>`), ⏭ Пропустить, 🛡 Protect; protected jobs get 🛠 Подготовить черновик
  instead (a manual application). Callback data uses the jobs rowid to stay under 64 bytes.
* **Rating** (S2 done-condition): `/rate` shows random labeled jobs one at a time with 👍/👎; votes are
  events, `/funnel` shows the agreement percentage.
* **Bot**: `/shortlist`, `/funnel`, `/sources`, `/rate`, `/protect`, `/unprotect`, `/dryrun`, `/reload`.
* **Digest**: labeled jobs rank by score above unlabeled ones and show `score/100 track`; skipped jobs
  are no longer listed.

## Consequences
* With free Gemini quotas, 600 labels a day is about 30 requests; raise `daily_max_jobs` only together
  with the provider limits in `llm.yaml`.
* Labels are only as good as the excerpt (700 characters); S3 may label shortlisted jobs again with the
  full text before tailoring.
* **No points from a rejected claim** (S2 review): `legal` is capped at the rubric's best value for
  the visa and remote claims that survived the quote check (`legal_cap`): a fabricated sponsorship
  quote next to a quoted "remote EMEA" is worth 12; nothing verified is worth "unknown" (8). An
  unquoted claim cannot push a job over the shortlist or alert threshold.
* A job the model leaves out of its answer 3 times is dropped with `drop_rule = label_missing`
  instead of costing tokens every run. `/rate` stops after the 30-label sample.
