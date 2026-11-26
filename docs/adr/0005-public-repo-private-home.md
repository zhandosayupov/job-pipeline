# 0005. Public repository, private data in JOBPIPE_HOME

Date: 2026-11-26 · Status: accepted

## Context
The owner wants the code public so others can reuse it. The system handles a CV, a fact base,
recruiter conversations, a list of target companies and API keys.

## Decision
* The repo contains code, prompts, tests with synthetic fixtures and `config.example/`.
* All personal data lives in `JOBPIPE_HOME` (default `~/.jobpipe`; VM `/var/lib/jobpipe`).
  Secrets live in `secrets.env` outside the repo.
* `tools/leak_guard.py` runs as a pre-commit hook and in CI. It blocks secret-shaped strings,
  private paths and every string listed in the private `$JOBPIPE_HOME/leakguard.txt`.
* `.gitignore` repeats the private paths as a second line of defence.

## Consequences
Test fixtures must be synthetic or scrubbed. Prompts must not embed personal facts; facts come from
the KB at run time.
