"""Triage of recruiter messages (S5): rules first, the LLM only for what rules cannot tell.

Categories:
  invite              an interview / call invitation
  screening_question  questions to answer before the process goes on
  info_request        asks for something (CV, portfolio, availability) without being an invite
  rejection           the process ended
  ack                 "we received your application" (confirms a submit)
  job_alert           a saved-search alert: vacancy links become postings
  spam / other        nothing to do

Sensitive topics are detected by regex as well as by the LLM, and the union is used: a topic the
model missed still forces the owner's Approve (ADR 0016).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from ..domain.message import InMessage
from .forms import TOPICS

Category = Literal["invite", "screening_question", "info_request", "rejection", "ack", "job_alert", "spam",
                   "other"]
NEEDS_REPLY = {"invite", "screening_question", "info_request"}

REJECTION = re.compile(
    r"not (?:to )?(?:be )?mov(?:e|ing) forward|decided to (?:move forward|proceed|go ahead) with other|"
    r"(?:pursue|proceed with|move forward with) other candidates|not (?:be )?(?:proceed|progress)(?:ing)? "
    r"(?:with|further)|position has (?:now )?been filled|regret to inform|will not be (?:taking|moving)|"
    r"not (?:been )?selected|unable to offer you|не готовы (?:пригласить|сделать|предложить)|"
    r"(?:приняли|принято) решение (?:не|в пользу другого)|к сожалению,? (?:мы )?(?:не (?:можем|готовы)|"
    r"вынуждены отказать|ваша кандидатура)|отказ(?:ом)? по (?:вакансии|вашему отклику)", re.I)
INVITE = re.compile(
    r"invite you (?:to|for) (?:an? )?(?:interview|call|chat|conversation|technical)|"
    r"(?:schedule|book|set up|arrange) (?:a|an|the|some) (?:time|call|interview|chat|meeting)|"
    r"calendly\.com|cal\.com/|goodtime\.io|availability for (?:a|an) (?:call|interview)|"
    r"(?:next step|next round) (?:is|would be) (?:a|an) (?:call|interview)|"
    r"приглаша\w* (?:вас )?на (?:собеседование|интервью|звонок|встречу)|приглашение на (?:собеседование|интервью)|"
    r"(?:назначить|согласовать) (?:время|звонок|собеседование)", re.I)
ACK = re.compile(
    r"(?:thank(?:s| you) for (?:applying|your application|your interest)|we(?:'ve| have) received your application|"
    r"application (?:has been )?received|received your application|"
    r"спасибо за (?:отклик|интерес к вакансии)|ваш отклик (?:получен|доставлен|отправлен)|"
    r"резюме (?:получено|доставлено))", re.I)
QUESTION_HINT = re.compile(
    r"(?:please (?:answer|complete|fill|let us know|share|send)|could you (?:please )?(?:share|send|tell|let)|"
    r"a few (?:quick )?questions|ответьте|расскажите|пришлите|подскажите)", re.I)
INFO_HINT = re.compile(r"(?:send (?:us )?your (?:cv|resume|portfolio)|пришлите (?:резюме|cv))", re.I)

# Message-only sensitive topics (application forms use forms.TOPICS).
EXTRA_TOPICS: list[tuple[str, re.Pattern]] = [
    ("interview_scheduling", re.compile(
        r"interview|schedule|availability|time slot|calendly|when (?:are|would) you (?:be )?(?:free|available)|"
        r"собеседовани|интервью|удобное время|созвон", re.I)),
    ("offer", re.compile(r"\boffer\b|оффер|предложение о работе", re.I)),
]
JOB_WORDS = re.compile(
    r"\b(?:interview|position|role|vacancy|opening|opportunit\w+|recruit\w*|hiring|candidate|application|"
    r"resume|cv|job|engineer|developer|talent)\b|вакансi?\w*|собеседовани\w*|резюме|рекрутер\w*|"
    r"отклик\w*|кандидат\w*|позици\w+", re.I)


def topics_of(text: str) -> set[str]:
    out = {t for t, rx in TOPICS if rx.search(text)}
    out |= {t for t, rx in EXTRA_TOPICS if rx.search(text)}
    return out


def questions_in(text: str, limit: int = 10) -> list[str]:
    """Sentences that end with a question mark, plus numbered list items under a question hint."""
    # Mail is hard-wrapped: join soft line breaks unless the next line starts a list item.
    text = re.sub(r"(?<!\n)\n(?!\n|\s*(?:\d+[.)]|[-•*])\s)", " ", text or "")
    out = []
    for s in re.split(r"(?<=[.!?])\s+|\n+", text):
        s = " ".join(s.split()).lstrip("-•*0123456789.) ")
        if s.endswith("?") and 8 <= len(s) <= 400 and s not in out:
            out.append(s)
    return out[:limit]


class TriageOut(BaseModel):
    category: Category
    questions: list[str] = Field(default_factory=list, max_length=15)
    topics: list[str] = Field(default_factory=list)
    summary: str = Field("", max_length=300)

    @field_validator("category", mode="before")
    @classmethod
    def _lower(cls, v):
        return v.strip().lower() if isinstance(v, str) else v


@dataclass
class Triage:
    category: str
    by: str                                   # rule:<name> | llm | fallback
    questions: list[str] = field(default_factory=list)
    topics: set[str] = field(default_factory=set)
    summary: str = ""


def _norm(s: str) -> str:
    return " ".join(re.sub(r"[^\w?]+", " ", s.lower()).split())


def host_matches(m: InMessage, patterns: list[str]) -> bool:
    addr, dom = m.from_addr.lower(), m.domain
    return any(addr == p.lower() or dom == p.lower() or dom.endswith("." + p.lower()) for p in patterns)


def is_job_related(m: InMessage, *, ats_domains: list[str], alert_senders: list[str], linked: bool,
                   ignore: list[str]) -> bool:
    """Only job-related mail is stored or sent to an LLM; the rest of the mailbox is never read further."""
    if host_matches(m, ignore):
        return False
    if linked or host_matches(m, ats_domains) or host_matches(m, alert_senders):
        return True
    return bool(JOB_WORDS.search(f"{m.subject}\n{m.text[:3000]}")) and not m.auto


def by_rules(m: InMessage, *, alert_senders: list[str]) -> Triage | None:
    text = " ".join(f"{m.subject}\n{m.text}".split())      # phrases broken by hard line wraps still match
    if host_matches(m, alert_senders) and (len([1 for h, _ in m.links if "/vacancy/" in h or "/jobs/view/" in h])
                                            >= 2 or re.search(r"новые вакансии|подходящие вакансии|job alert|"
                                                              r"jobs? (?:for|matching) you", text, re.I)):
        return Triage("job_alert", "rule:alert")
    if REJECTION.search(text):
        return Triage("rejection", "rule:rejection")
    if INVITE.search(text):
        return Triage("invite", "rule:invite", questions_in(m.text), topics_of(text) | {"interview_scheduling"})
    if ACK.search(text) and not QUESTION_HINT.search(text) and not questions_in(m.text):
        return Triage("ack", "rule:ack")
    return None


def check_llm(out: TriageOut, m: InMessage) -> Triage:
    """Keep only questions that are really in the message; topics are the union with the regex ones."""
    body = _norm(m.text)
    qs = [q for q in out.questions if _norm(q) and _norm(q) in body]
    text = f"{m.subject}\n{m.text}"
    topics = topics_of(text) | {t for t in out.topics if t in {x for x, _ in TOPICS} | {x for x, _ in EXTRA_TOPICS}}
    cat = out.category
    if cat == "screening_question" and not qs:
        qs = questions_in(m.text)
        if not qs:
            cat = "info_request"
    return Triage(cat, "llm", qs, topics, out.summary)


def fallback(m: InMessage) -> Triage:
    """No LLM: a message with questions is a screening; else 'other' (the owner is told about it)."""
    qs = questions_in(m.text)
    text = f"{m.subject}\n{m.text}"
    if INFO_HINT.search(text):
        return Triage("info_request", "fallback", qs, topics_of(text))
    if qs:
        return Triage("screening_question", "fallback", qs, topics_of(text))
    return Triage("other", "fallback", [], topics_of(text))
