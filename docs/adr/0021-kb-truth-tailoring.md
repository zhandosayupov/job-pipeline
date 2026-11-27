# 0021. Knowledge base, truth gate, tailoring

Date: 2026-11-27 · Status: accepted (S3) · Implements ADR 0008

## Decision
* **Knowledge base** in `$JOBPIPE_HOME/kb/` (never the repo): `profile.yaml` (name, contacts,
  links, education, skills: pseudonymized before LLM calls), `facts.yaml` (`roles` and `facts`; each
  fact has an id, a canonical statement, allowed `phrasings`, `role`, `kind`, `contribution`
  me/team/led, `tech`, `tags`), `answers.yaml` (verbatim answers to recurring form questions with
  regex `question_patterns`). Validated on load (duplicate ids, unknown roles). `jobpipe kb check`
  runs the truth gate over the KB's own wordings and lists skills no fact shows.
  `config.example/kb/` is a made-up person used by tests; it is never copied into a real home.
* **Truth gate** (`services/truth.py`), deterministic. Claims extracted from generated text:
  * numbers and percentages, normalised so that 2B, 2 billion and 2000000000 are equal;
  * years;
  * "N years of experience", compared against the work history;
  * technologies (a vocabulary), allowed when the sentence admits a gap ("I have not used Rust");
  * employers in phrases like "worked at X";
  * self-titles in phrases like "as a Senior ...";
  * degrees.

  Each claim must match the KB. The target company and job title are allowed as plain mentions.
* **Tailoring** (`services/tailor.py`, `jobpipe run tailor`, hourly 08–23):
  1. **Plan.** The LLM (task `tailor`, personal) returns a plan of fact ids and wording indexes, a skills order, requirement → fact matches and gaps. Unknown ids are dropped. If fewer than 3 bullets survive, a deterministic plan ranked by overlap with the job's tech and tags is used instead.
  2. **Summary.** It is kept only if the gate passes; otherwise the headline replaces it.
  3. **Cover letter.** The LLM (task `cover_letter`, ≤ 150 words, RU for RU postings) writes it. If the gate finds claims, there is one retry that names the claims to avoid. If it still fails, the draft is **blocked**: the owner is asked about each claim and the draft is not retried until they press «Переделать».
  4. **Rendering.** LaTeX from `templates/cv.tex`, all text escaped. With `tectonic` the PDF is compiled and bullets are dropped until it fits one page; without it the `.tex` is delivered.
  5. **Approval.** Artifacts go to `data/artifacts/<app>/` with a diff against the canonical CV. The state moves `draft → tailoring → ready` and an approval card is sent: ✅ Одобрить sets `approved_cv`, 🔁 Переделать goes `ready → tailoring → draft`, 🗑 Отменить sets `withdrawn`.
* The personal LLM calls are pseudonymized with the KB's own values (name, email, phone, links) on
  top of `llm.yaml: pii_values`.

## Consequences
* With only free Gemini (`trains_on_data: true`), personal calls are refused unless the owner sets
  `personal_on_training_providers: true` (with `pii_redaction: required`, the default) or adds a
  provider that does not train on prompts. Until then tailoring falls back to the deterministic plan
  and blocks on the missing letter.
* The owner's own LaTeX templates (`cv_*_hft.tex`, `cv_*_generic.tex`) can replace `templates/cv.tex`
  once they use the same placeholders; see `docs/TODO.md`.
* **Known limit: the technology list is closed.** The gate checks technologies from a fixed list
  (`TECH` in `services/truth.py`). A technology outside it (Tokio, a niche library) produces no
  claim and cannot be blocked. A negation exempts a technology only when it is a negation of using
  it, right before it ("I have not used Rust"), not anywhere in the sentence (S3 review).
