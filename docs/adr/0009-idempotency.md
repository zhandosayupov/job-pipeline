# 0009. Idempotency and cooldowns

Date: 2026-11-26 · Status: accepted

## Context
The spec required one application per (company, role family) per cooldown window. Most companies
publish no cooldown, and "role family" was undefined.

## Decision
* Hard rule: one application per (company, vacancy) ever. The key is
  `apply:<company_key>:<vacancy_key>`, stored before any action.
* Cooldown only when configured for a company (`cooldown_days` or `until`); none by default.
* An existing application to a similar role at the same company (same track and level) adds a
  warning to the approval card. The owner decides; nothing is blocked.
* Every external side effect has an idempotency key in `side_effects` written before execution.
