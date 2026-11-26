"""`jobpipe run tailor`: application drafts -> tailored CV + cover letter -> approval card (S3, spec §8).

For each application in `draft`:
  1. plan (LLM task `tailor`, personal, pseudonymized): which facts, which allowed wording, skills
     order, requirement -> fact matches, gaps. Unknown fact ids are dropped; if fewer than 3 bullets
     survive, a deterministic plan (tag/tech overlap with the job) is used instead.
  2. summary: kept only if the truth gate finds no unmatched claim, else replaced by the headline.
  3. cover letter (LLM task `cover_letter`, <= 150 words, RU for RU postings): truth-gated; one retry
     with the unmatched claims named; if it still fails, the application is blocked and the owner is
     asked about each claim.
  4. render LaTeX (PDF with tectonic when installed, trimmed to one page), diff against the canonical
     CV (every fact's statement), artifacts in $JOBPIPE_HOME/data/artifacts/<app>/.
  5. draft -> tailoring -> ready, and an approval card: Одобрить / Переделать / Отменить.
"""
from __future__ import annotations

import difflib
import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, Field

from ..clock import Clock, iso
from ..config.settings import AppConfig
from ..ports.notifier import Notifier
from . import control
from .events import transition
from .kb import KnowledgeBase
from .llm_router import LLMUnavailable
from .render import CvContent, render_one_page
from .truth import TruthGate, TruthReport

log = logging.getLogger(__name__)
MAX_LETTER_WORDS = 150


class PlanBullet(BaseModel):
    fact_id: str
    phrasing: int = 0


class Requirement(BaseModel):
    requirement: str
    fact_ids: list[str] = Field(default_factory=list)


class TailorPlan(BaseModel):
    variant: str = "generic"
    summary: str = ""
    bullets: list[PlanBullet] = Field(default_factory=list)
    skills_order: list[str] = Field(default_factory=list)
    requirements: list[Requirement] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)


class Letter(BaseModel):
    letter: str


@dataclass
class TailorResult:
    app_id: str
    ok: bool
    blocked: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    artifacts: dict[str, str] = field(default_factory=dict)


# --- deterministic helpers -----------------------------------------------------------------


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-zа-я0-9+#]+", text.lower()) if len(w) > 1}


def fallback_plan(kb: KnowledgeBase, job_text: str, n: int = 8) -> TailorPlan:
    """Rank facts by overlap of their tech/tags/wording with the job; no LLM involved."""
    jw = _words(job_text)

    def score(f) -> tuple:
        tech = sum(t.lower() in job_text.lower() for t in f.tech)
        tags = sum(t.lower() in jw for t in f.tags)
        words = len(_words(f.statement) & jw)
        return (tech * 3 + tags * 2 + words, f.kind == "achievement")

    ranked = sorted(kb.facts, key=score, reverse=True)
    return TailorPlan(variant="generic", bullets=[PlanBullet(fact_id=f.id) for f in ranked[:n]])


def clean_plan(kb: KnowledgeBase, plan: TailorPlan, job_text: str) -> tuple[TailorPlan, list[str]]:
    notes, seen, bullets = [], set(), []
    for b in plan.bullets:
        f = kb.fact(b.fact_id)
        if f is None:
            notes.append(f"план ссылался на несуществующий факт {b.fact_id!r}: убран")
            continue
        if b.fact_id in seen:
            continue
        seen.add(b.fact_id)
        idx = b.phrasing if 0 <= b.phrasing < len(f.wordings()) else 0
        bullets.append(PlanBullet(fact_id=b.fact_id, phrasing=idx))
    plan = plan.model_copy(update={"bullets": bullets[:10]})
    if len(plan.bullets) < 3:
        notes.append("план LLM слишком короткий: взят детерминированный план по пересечению с вакансией")
        plan = fallback_plan(kb, job_text).model_copy(update={"summary": plan.summary,
                                                              "skills_order": plan.skills_order,
                                                              "requirements": plan.requirements,
                                                              "gaps": plan.gaps})
    known = {s.lower() for items in kb.profile.skills.values() for s in items}
    plan.skills_order = [s for s in plan.skills_order if s.lower() in known]
    return plan, notes


def canonical_lines(kb: KnowledgeBase) -> list[str]:
    return [f.statement for f in kb.facts]


def cv_diff(kb: KnowledgeBase, chosen: list[str]) -> list[str]:
    """What the tailored CV shows vs the canonical one: '+' reworded, '-' left out, order kept."""
    canon = canonical_lines(kb)
    return [ln for ln in difflib.unified_diff(canon, chosen, lineterm="", n=0)
            if ln[:1] in "+-" and not ln.startswith(("+++", "---"))]


def _job_text(row: sqlite3.Row) -> str:
    data = json.loads(row["data_json"] or "{}")
    return "\n".join(filter(None, [row["title"], row["company"], row["location"], data.get("description")]))[:4000]


def _facts_json(kb: KnowledgeBase) -> str:
    return json.dumps([{"id": f.id, "role": f.role, "wordings": f.wordings(), "tech": f.tech, "tags": f.tags,
                        "contribution": f.contribution} for f in kb.facts], ensure_ascii=False)


# --- the pipeline step ----------------------------------------------------------------------


def tailor_one(conn: sqlite3.Connection, clock: Clock, cfg: AppConfig, kb: KnowledgeBase, router,
               app: sqlite3.Row, job: sqlite3.Row) -> TailorResult:
    res = TailorResult(app["id"], ok=False)
    job_text = _job_text(job)
    job_data = json.loads(job["data_json"] or "{}")
    lang = "ru" if job_data.get("lang") == "ru" else "en"
    gate = TruthGate(kb, allowed_extra=[job["company"] or "", job["title"] or ""])

    # 1. plan
    try:
        out = router.complete("tailor", {
            "job": job_text, "headline": kb.profile.headline,
            "skills": json.dumps(kb.profile.skills, ensure_ascii=False),
            "roles": json.dumps([r.model_dump() for r in kb.roles], ensure_ascii=False),
            "facts": _facts_json(kb),
        }, schema=TailorPlan, personal=True)
        plan = out.data
    except LLMUnavailable as e:
        res.notes.append(f"LLM недоступен для плана ({str(e)[:120]}): детерминированный план")
        plan = TailorPlan()
    plan, notes = clean_plan(kb, plan, job_text)
    res.notes += notes

    # 2. summary
    summary = plan.summary.strip()
    if summary:
        rep = gate.check(summary)
        if not rep.ok:
            res.notes.append("summary заменён заголовком: " + _claims(rep))
            summary = kb.profile.headline
    else:
        summary = kb.profile.headline

    # 3. letter
    bullets = [(b.fact_id, kb.fact(b.fact_id).wordings()[b.phrasing]) for b in plan.bullets]
    used = {fid for fid, _ in bullets} | {fid for r in plan.requirements for fid in r.fact_ids}
    letter, letter_rep = _letter(router, kb, gate, job_text, plan, used, lang)
    if letter_rep is not None and not letter_rep.ok:
        res.blocked = [f"{c.kind}: {c.text}" for c in letter_rep.unmatched]
    if letter is None:
        res.blocked = res.blocked or ["письмо не получено от LLM"]

    # 4. render
    out_dir = Path(cfg.paths.data_dir) / "artifacts" / re.sub(r"[^\w.-]+", "_", app["id"])
    tex, pdf = render_one_page(kb, CvContent(summary, bullets, plan.skills_order), out_dir)
    res.artifacts = {"tex": str(tex)}
    if pdf:
        res.artifacts["pdf"] = str(pdf)
    if letter:
        (out_dir / "letter.txt").write_text(letter, encoding="utf-8")
        res.artifacts["letter"] = str(out_dir / "letter.txt")
    diff = cv_diff(kb, [w for _, w in bullets])
    (out_dir / "plan.json").write_text(json.dumps({
        "plan": plan.model_dump(), "summary": summary, "diff": diff, "notes": res.notes, "blocked": res.blocked,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    res.artifacts["plan"] = str(out_dir / "plan.json")
    res.ok = not res.blocked
    return res


def _letter(router, kb, gate, job_text, plan, used, lang) -> tuple[str | None, TruthReport | None]:
    facts = [f for f in kb.facts if f.id in used] or kb.facts
    variables = {
        "job": job_text[:2500], "language": "Russian" if lang == "ru" else "English",
        "matches": json.dumps([r.model_dump() for r in plan.requirements], ensure_ascii=False),
        "gaps": json.dumps(plan.gaps, ensure_ascii=False),
        "facts": json.dumps([{"id": f.id, "wordings": f.wordings(), "tech": f.tech} for f in facts],
                            ensure_ascii=False),
    }
    letter, rep = None, None
    for attempt in range(2):
        try:
            out = router.complete("cover_letter", variables, schema=Letter, personal=True)
        except LLMUnavailable:
            return None, None
        letter = " ".join(out.data.letter.split()) if lang == "en" else out.data.letter.strip()
        words = letter.split()
        if len(words) > MAX_LETTER_WORDS + 20:
            letter = " ".join(words[:MAX_LETTER_WORDS]).rstrip(",;") + "."
        rep = gate.check(letter)
        if rep.ok:
            return letter, rep
        if attempt == 0:
            variables["gaps"] = json.dumps(plan.gaps + [
                f"DO NOT mention: {c.text} (not in the facts)" for c in rep.unmatched], ensure_ascii=False)
    return letter, rep


def _claims(rep: TruthReport) -> str:
    return ", ".join(f"{c.kind} «{c.text}»" for c in rep.unmatched)


def run(conn: sqlite3.Connection, clock: Clock, cfg: AppConfig, kb: KnowledgeBase, router, notifier: Notifier,
        limit: int = 10) -> list[TailorResult]:
    """Tailor up to `limit` drafts and send their approval cards."""
    from .cards import send_application_card

    results = []
    if control.is_paused(conn):
        return results
    apps = conn.execute(
        "SELECT rowid, * FROM applications WHERE state = 'draft' "
        "AND json_array_length(COALESCE(json_extract(data_json, '$.blocked'), '[]')) = 0 "
        "ORDER BY created_at LIMIT ?", (limit,)).fetchall()
    for app in apps:
        job = conn.execute("SELECT * FROM jobs WHERE id = ?", (app["job_id"],)).fetchone()
        if job is None:
            continue
        transition(conn, clock, "application", app["id"], "tailoring", expected="draft", actor="tailor")
        try:
            r = tailor_one(conn, clock, cfg, kb, router, app, job)
        except Exception as e:
            transition(conn, clock, "application", app["id"], "draft", expected="tailoring", actor="tailor",
                       data={"error": f"{type(e).__name__}: {str(e)[:300]}"})
            log.exception("tailoring failed")
            notifier.send(f"Тейлоринг {job['company']} — {job['title']} упал: {type(e).__name__}: {str(e)[:200]}")
            continue
        data = json.loads(app["data_json"] or "{}")
        data.update({"artifacts": r.artifacts, "notes": r.notes, "blocked": r.blocked,
                     "tailored_at": iso(clock.now())})
        conn.execute("UPDATE applications SET data_json = ? WHERE id = ?",
                     (json.dumps(data, ensure_ascii=False), app["id"]))
        if r.blocked:
            transition(conn, clock, "application", app["id"], "draft", expected="tailoring", actor="tailor",
                       data={"blocked": r.blocked})
            data["blocked_notified"] = True
            conn.execute("UPDATE applications SET data_json = ? WHERE id = ?",
                         (json.dumps(data, ensure_ascii=False), app["id"]))
            # A blocked draft is not retried automatically (that would repeat the same question every
            # run): the owner adds the missing facts to kb/facts.yaml and presses «Переделать».
            notifier.send(
                f"⛔ Проверка правды остановила письмо для {job['company']} — {job['title']}.\n"
                "Утверждения без факта в базе:\n" + "\n".join(f"• {b}" for b in r.blocked)
                + "\nЕсли это правда — добавь факт в kb/facts.yaml и нажми «Переделать»; "
                "иначе заявка останется черновиком.",
                buttons=[[("🔁 Переделать", f"a:redo:{app['rowid']}"), ("🗑 Отменить", f"a:cancel:{app['rowid']}")]])
        else:
            transition(conn, clock, "application", app["id"], "ready", expected="tailoring", actor="tailor")
            send_application_card(conn, notifier, app["id"])
        results.append(r)
    return results
