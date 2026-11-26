# 0008. Tailoring selects facts; personal data is pseudonymized

Date: 2026-11-26 · Status: accepted (implemented in S3)

## Decision
* A pool of base CV variants (for example hft, backend, generic, ru) built from the owner's templates.
* The LLM returns a plan, not prose: variant id, ordered `fact_id`s, chosen allowed-phrasing ids,
  skills order. A renderer turns the plan into LaTeX and compiles it with tectonic.
* Free text (summary, cover letter) is checked by the truth validator: every employer, title, date,
  number, technology and degree must match a fact; an unmatched claim blocks the item.
* Before any LLM call marked personal, configured personal values (name, email, phone, links) are
  replaced with placeholders and restored locally afterwards. Controlled by
  `pii_redaction: required | off` in `llm.yaml`.
* Recurring form questions are answered from `kb/answers.yaml`; a new question goes to the owner,
  and the answer is offered for saving.
