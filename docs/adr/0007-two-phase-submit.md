# 0007. Two-phase submit with a fill manifest

Date: 2026-11-26 · Status: accepted (implemented in S4) · Amended by [0016](0016-auto-apply.md):
applications are auto-submitted when every check passes; this two-phase flow is the fallback
for applications that need the owner.

## Context
The owner approves from a phone, sometimes hours later. Holding a filled form open that long is fragile.

## Decision
* Phase 1 (`filling`): open, fill, upload, screenshot, store a manifest (field → value) and its
  hash, close the tab. The approval card shows the screenshot and the manifest.
* Phase 2 (`submitting`): after Approve, refill and recompute the hash. Equal: submit. Different
  (new question, changed form): back to `awaiting_approval` with the new manifest.
* A `side_effects` row is written as `planned` before the submit click. If the worker dies between
  the click and the confirmation page, the application becomes `submit_unknown` and the owner is
  asked; there is no automatic retry. Submission is at most once by construction.
