"""Reply drafts for recruiter messages (S5). Built from the KB only and never sent by code.

* Each question is matched to a topic. Sensitive topics are never answered with generated text:
  the owner's verbatim answer from kb/answers.yaml (policy `preapproved`), the visa rule from the
  job's country (`work_authorization`), or a placeholder `[[ВПИШИ: ...]]` the owner fills in.
  Interview times and offers are always placeholders.
* The LLM (task `reply`, pseudonymized) only writes the connecting text around those fixed answers.
  Every fixed answer and placeholder must appear in its output, else it is appended; the rest goes
  through the truth gate, and a draft that fails it cannot be approved.
* Without an LLM a plain template is used: greeting, the fixed answers, the placeholders, signature.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from ..config.settings import PolicyCfg
from .forms import topic_of
from .kb import KnowledgeBase
from .llm_router import LLMUnavailable
from .policy import needs_sponsorship
from .triage import EXTRA_TOPICS
from .truth import TruthGate, TruthReport

MAX_WORDS = 220
SLOT_HOLE = "[[ВПИШИ: удобное время для звонка]]"
PLACEHOLDER = re.compile(r"\[\[[^\]]{1,80}\]\]")
TOPIC_RU = {"compensation": "зарплатные ожидания", "notice_period": "срок выхода", "start_date": "дата выхода",
            "visa": "виза/разрешение на работу", "citizenship": "гражданство", "references": "рекомендации",
            "reason_for_leaving": "причина ухода", "interview_scheduling": "удобное время для звонка",
            "offer": "ответ по офферу", "relocation": "переезд"}


class ReplyOut(BaseModel):
    reply: str = Field(..., max_length=4000)


@dataclass
class Draft:
    text: str
    lang: str
    generated: bool                                   # False: template, no LLM
    fixed: list[str] = field(default_factory=list)    # verbatim answers inserted as they are
    placeholders: list[str] = field(default_factory=list)
    topics: set[str] = field(default_factory=set)
    truth: TruthReport | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.truth is None or self.truth.ok


def lang_of(text: str) -> str:
    cyr = len(re.findall(r"[а-яё]", text.lower()))
    lat = len(re.findall(r"[a-z]", text.lower()))
    return "ru" if cyr > lat else "en"


def _question_topic(q: str) -> str | None:
    t = topic_of(q)
    if t:
        return t
    for name, rx in EXTRA_TOPICS:
        if rx.search(q):
            return name
    return None


def _visa_sentence(policy: PolicyCfg, country: str | None, lang: str) -> str | None:
    need = needs_sponsorship(policy, country)
    if need is None:
        return None
    if lang == "ru":
        return ("Для работы в этой стране мне понадобится визовая поддержка." if need
                else "Виза или спонсорство для работы в этой стране мне не нужны.")
    return ("I would need visa sponsorship to work in this location." if need
            else "I do not need visa sponsorship to work in this location.")


def answers_for(questions: list[str], kb: KnowledgeBase, policy: PolicyCfg, *, job_country: str | None,
                lang: str) -> tuple[list[tuple[str, str]], list[str], set[str]]:
    """([(question, verbatim answer)], placeholders, topics) for the questions of a message."""
    fixed, holes, topics = [], [], set()
    for q in questions:
        t = _question_topic(q)
        if t:
            topics.add(t)
        sensitive = t in set(policy.sensitive_topics)
        mode = policy.sensitive_answers.get(t or "", policy.sensitive_answers.get("default", "preapproved"))
        answer = None
        if t in ("interview_scheduling", "offer"):
            answer = None
        elif sensitive and mode == "work_authorization" and t == "visa":
            answer = _visa_sentence(policy, job_country, lang)
        elif sensitive and mode == "work_authorization" and t == "citizenship":
            answer = kb.profile.citizenship or None
        elif sensitive and mode in ("ask", "posting_range"):
            answer = None   # a message is not a form: the owner writes the number
        else:
            a = kb.answer_for(q)
            if a:
                answer = a.answer_ru if lang == "ru" and a.answer_ru else a.answer
        if answer:
            fixed.append((q, answer))
        elif sensitive or t in ("interview_scheduling", "offer"):
            holes.append(f"[[ВПИШИ: {TOPIC_RU.get(t, t)}]]")
    return fixed, list(dict.fromkeys(holes)), topics


def _first_name(name: str) -> str:
    n = re.sub(r"[\"'<>()]", " ", name or "").split()
    return n[0] if n and not re.search(r"@|\bteam\b|no.?reply|recruit(?:ing|ment)\b|\bhr\b|talent|careers|hiring",
                                                               name or "", re.I) else ""


def template(kb: KnowledgeBase, *, lang: str, to_name: str, fixed: list[str], holes: list[str],
             interview: bool) -> str:
    first = _first_name(to_name)
    if lang == "ru":
        parts = [f"Здравствуйте{', ' + first if first else ''}!", "Спасибо за сообщение."]
        if interview:
            parts.append("С удовольствием пообщаюсь.")
        parts += fixed + holes + [f"С уважением,\n{kb.profile.name}"]
    else:
        parts = [f"Hello{' ' + first if first else ''},", "Thank you for your message."]
        if interview:
            parts.append("I would be glad to talk.")
        parts += fixed + holes + [f"Best regards,\n{kb.profile.name}"]
    return "\n\n".join(parts)


def _norm(s: str) -> str:
    return " ".join(re.sub(r"[^\w\[\]:]+", " ", s.lower()).split())


def build(kb: KnowledgeBase, policy: PolicyCfg, router, *, message: str, subject: str, to_name: str,
          category: str, questions: list[str], topics: set[str], company: str | None, job_title: str | None,
          job_country: str | None, slots: list[str] | None = None) -> Draft:
    lang = lang_of(f"{subject}\n{message}")
    qa, holes, q_topics = answers_for(questions, kb, policy, job_country=job_country, lang=lang)
    if category == "invite" and SLOT_HOLE not in holes:
        holes.append(SLOT_HOLE)
    fixed = [a for _, a in qa]
    if slots and SLOT_HOLE in holes:
        # Free times from the owner's own calendar (S7): a fixed answer, not generated text.
        holes.remove(SLOT_HOLE)
        intro = "Мне удобно в любое из этих окон:" if lang == "ru" else "I am available at any of these times:"
        tail = ("Если не подходит, предложите своё время." if lang == "ru"
                else "If none of them works, I am happy to adapt.")
        fixed.append("\n".join([intro, *[f"- {x}" for x in slots], tail]))
    all_topics = set(topics) | q_topics
    gate = TruthGate(kb, allowed_extra=[x for x in (company, job_title, to_name, subject, *fixed) if x])
    draft = Draft(text="", lang=lang, generated=False, fixed=fixed, placeholders=holes, topics=all_topics)

    text = None
    if router is not None:
        variables = {
            "language": "Russian" if lang == "ru" else "English",
            "category": category,
            "message": message[:3000],
            "company": company or "unknown",
            "role": job_title or "unknown",
            "first_name": _first_name(to_name) or "(none)",
            "fixed": json.dumps([{"question": q, "answer": a} for q, a in qa], ensure_ascii=False),
            "placeholders": json.dumps(holes, ensure_ascii=False),
            "facts": json.dumps([{"id": f.id, "wordings": f.wordings()} for f in kb.facts], ensure_ascii=False),
            "name": kb.profile.name,
        }
        for attempt in range(2):
            try:
                out = router.complete("reply", variables, schema=ReplyOut, personal=True)
            except LLMUnavailable:
                draft.notes.append("LLM недоступен: черновик по шаблону")
                break
            text = _complete(out.data.reply.strip(), fixed, holes)
            words = text.split()
            if len(words) > MAX_WORDS + 40:
                draft.notes.append(f"длинный ответ ({len(words)} слов)")
            rep = gate.check(_without(text, fixed))
            draft.truth, draft.generated = rep, True
            if rep.ok:
                break
            if attempt == 0:
                variables["facts"] += "\nDO NOT mention: " + ", ".join(c.text for c in rep.unmatched)
    if text is None:
        text = template(kb, lang=lang, to_name=to_name, fixed=fixed, holes=holes, interview=category == "invite")
        draft.truth = gate.check(_without(text, fixed))
    draft.text = text
    return draft


def _complete(text: str, fixed: list[str], holes: list[str]) -> str:
    """Append any fixed answer or placeholder the model dropped or rephrased."""
    missing = [x for x in fixed + holes if _norm(x) not in _norm(text)]
    if not missing:
        return text
    sig = re.search(r"\n\s*(?:best regards|kind regards|regards|thanks|с уважением|спасибо)[,!.]?\s*\n", text, re.I)
    add = "\n\n".join(missing)
    if sig:
        return text[: sig.start()].rstrip() + "\n\n" + add + "\n" + text[sig.start():]
    return text.rstrip() + "\n\n" + add


def _without(text: str, fixed: list[str]) -> str:
    """The generated part only: verbatim KB answers and placeholders are not claims to check."""
    for x in fixed:
        text = text.replace(x, " ")
    return PLACEHOLDER.sub(" ", text)
