# 0016. Applications are submitted automatically when every check passes

Date: 2026-11-26 · Status: accepted · Amends: 0007 (the approval step becomes the fallback)

## Context
The original spec made every application L1: fill, show the owner a card, wait for Approve. At
20 applications a day that makes the owner the bottleneck, and the owner asked for auto-apply.

## Decision
* `policy.yaml` sets `autonomy.apply: L2` by default. `services/policy.decide_apply()` routes each
  application, in this order:
  1. **block**: already applied to this vacancy (idempotency key exists), or the truth gate found
     a claim with no matching fact;
  2. **defer**: kill switch on, or the platform's daily cap reached;
  3. **dry run**: dry run is still on for the platform (the default until the owner flips it);
  4. **ask** (L1 card): protected company, any form question without an answer in
     `kb/answers.yaml`, a similar application at the same company, or apply autonomy below L2;
  5. otherwise **submit** (L2).
* Auto path: `ready → filling → submitting → applied`. The fill manifest and screenshot are still
  stored, and the owner gets a report after each submission (and a daily digest), with a Withdraw
  button where the platform allows it.
* The L1 path stays as in ADR 0007: `filling → awaiting_approval → submitting` with a refill and
  manifest hash check.
* Messages to people (replies, email drafts) stay L1. They may be raised to L2 only after
  `l2_min_clean_approvals` approvals without edits, and never for `sensitive_topics`.
* Every submit still reserves its `side_effects` key first and checks the pause flag just before
  the click; `submit_unknown` still goes to the owner.

## Consequences
* Answers to recurring form questions (salary expectations, notice, work authorization, visa) must
  be pre-approved in `kb/answers.yaml`; until they are, those applications fall back to cards.
* A wrong application cannot be unsent. Daily caps, dry-run-first per platform and the protected
  pool limit the damage; S4 starts with dry runs on every platform.
