# 0019. Collection: sources, prefilter, dedupe, digest

Date: 2026-11-27 · Status: accepted (S1)

## Context
S1 turns public job boards into a daily Telegram digest. The VM is small, the sources are many and
uneven, and the owner must never get the same vacancy twice.

## Decision
* **Sources** are classes with `name`, `kind` (`ats` | `aggregator` | `community`) and
  `fetch(http, ctx) -> FetchOutcome`, registered in `adapters/sources/__init__.py` and switched on in
  `sources.yaml`. Each is made of units (a company board, a search query, a channel); a failing unit
  makes the source `partial`, all failing makes it `blocked` or `error`; one source never stops another.
  S1 ships Greenhouse, Ashby, Lever (company boards from `companies.yaml`), Amazon, Himalayas, Jobicy,
  We Work Remotely, Arbeitnow, HN "Who is hiring" and public Telegram channels (`t.me/s`, no login).
  Each has a fixture recorded from a live response (scrubbed of third-party emails and phones) and a
  contract test.
* **HTTP** (`adapters/http.py`): one User-Agent naming the project, robots.txt per RFC 9309 (4xx means
  no rules, 5xx or unreachable means disallow all), at most one request per second per host, two
  retries on 5xx/timeouts. 401/403/429, a captcha page or a robots refusal raise `Blocked`: the source
  stops for that run and the owner is told **at once** (the first blocked run; CLAUDE.md). Plain errors
  alert after `health_alert_after` consecutive failures; both alert once more on recovery. Nothing is
  worked around (ADR 0012).
* **Companies' own career sites** (`adapters/sources/careers.py`). Most large employers publish
  through an ATS: 109 Greenhouse/Ashby/Lever boards are in `companies.yaml`. Sites without one are
  read the way search engines read them, or not at all:
  * `careers`: the vacancy sitemap plus the schema.org `JobPosting` JSON-LD on each page (Kaspi.kz,
    Meta). The sitemap lists every open vacancy, which gives closures; at most `max_new` pages are
    fetched per site per run, and seen pages are remembered.
  * `workday`: the public JSON search of Workday career sites (Nvidia), off by default.
  * A site whose robots.txt disallows its job pages (Google's job search) is not collected.
* **Re-seen postings** only refresh `last_seen`: the description stored at first sight is kept (the
  label and dedupe fingerprint stay stable), and a closed job that reappears stays closed (its id is
  known; a re-posted vacancy usually gets a new id and comes in as new).
* **Crash safety**: a job left in `discovered` by a crash between create and prefilter is finished by
  `sweep_discovered()` at the start of the next run.
* **Normalisation**: every source returns `Posting`; unknown fields stay `None`. Countries become
  ISO2 only when the source names one. Postings keep up to 8,000 characters of plain description.
* **Prefilter** (deterministic, `filters.yaml`, EN + RU): `title_not_eng`, `title_excluded` (with
  exceptions), `text_excluded`, `location_ru_by` (onsite only), `no_sponsorship` (an explicit refusal,
  unless the job is remote or in a country from `work_authorized_countries`). Visa detection is
  negation-aware and ignores export-control boilerplate ("without sponsorship for an export licence"),
  a false positive found on live Cloudflare postings. Every drop records its rule and keyword
  (`jobpipe jobs dropped`).
* **Dedupe** by fingerprint `company|title|city` (company suffixes such as B.V. and Inc removed): an ATS
  record beats an aggregator, which beats a community post. A copy already sent in a digest is never
  replaced. Postings without a company (Telegram) are not merged: the fingerprint would be too weak.
* **Closures**: company boards list every open job, so a job missing from a later successful fetch of
  its board becomes `closed`.
* **Digest** (09:00 Almaty): every job that passed the prefilter and was never sent, ranked by visa
  signal, remote, Kazakhstan, ATS origin and C++ in the title; the same role in several countries is
  one line with the other locations. Jobs are marked `digested_at` only after Telegram accepted every
  message, so a failed send is retried next time and nothing is sent twice. At most
  `digest_max_items` lines; the rest is counted (S2 labeling will rank them properly).
* **Schedule**: collect at 08, 11, 14, 17, 20, 23 Almaty (with up to 5 minutes of random delay),
  digest at 09:00. `jobpipe run collect --dry-run` works on a copy of the database.

## Consequences
* A first live run (2026-11-27, laptop, 10 sources) took 76 s: 2,753 postings fetched, 982 passed the
  prefilter, 73 merged as duplicates. That is too many to read: S2 labeling has to rank them.
* LinkedIn (jobspy pulls in pandas, too heavy for 1 GB), relocate.me (HTML, its robots.txt allows the
  listing pages) and Habr Career are left for later; see `docs/TODO.md`.
