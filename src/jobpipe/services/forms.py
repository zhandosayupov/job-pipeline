"""Answers for application form fields, resolved on the VM where the KB and the policy live (S4).

The worker scans a form and sends its fields here; this module decides every value or marks the field
unanswered. Nothing is generated: values come from the profile, the CV/letter artifacts, verbatim
answers.yaml entries, or the policy's work-authorization rule (ADR 0016). Unknown means unanswered,
which turns the application into an approval card.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field

from ..config.settings import PolicyCfg
from .kb import KnowledgeBase
from .policy import needs_sponsorship


class FormField(BaseModel):
    key: str                                   # selector or name the worker can fill again
    label: str
    kind: Literal["text", "textarea", "email", "tel", "url", "select", "radio", "checkbox", "file"] = "text"
    required: bool = False
    options: list[str] = Field(default_factory=list)


class FieldAnswer(BaseModel):
    key: str
    value: str
    source: Literal["profile", "kb_answer", "policy", "cv", "letter", "posting"]
    topic: str | None = None


@dataclass
class Resolution:
    answers: list[FieldAnswer] = field(default_factory=list)
    unanswered: list[FormField] = field(default_factory=list)
    topics_asked: set[str] = field(default_factory=set)
    topics_preapproved: set[str] = field(default_factory=set)

    def manifest(self) -> list[dict]:
        return sorted(({"key": a.key, "value": a.value} for a in self.answers), key=lambda d: d["key"])

    def manifest_hash(self) -> str:
        """Covers answered AND unanswered fields: a new question on the form must change the hash."""
        return manifest_hash(self.manifest() + [{"key": f.key, "value": None} for f in self.unanswered])


def manifest_hash(manifest: list[dict]) -> str:
    blob = json.dumps(sorted(manifest, key=lambda d: d["key"]), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


TOPICS: list[tuple[str, re.Pattern]] = [
    ("compensation", re.compile(r"salary|compensation|expected (?:pay|total|base)|pay expectation|desired pay|"
                                r"зарплат|заработн|оклад|ожидани\w+ по (?:доходу|зарплате)", re.I)),
    ("visa", re.compile(r"visa|sponsor|work permit|authori[sz]ed to work|eligib\w+ to work|right to work|"
                        r"legally (?:able|permitted) to work|виз\w*|разрешени\w+ на работу", re.I)),
    ("citizenship", re.compile(r"citizenship|nationality|гражданств", re.I)),
    ("notice_period", re.compile(r"notice period|when can you start|start date|earliest start|availability|"
                                 r"когда (?:сможете|готовы) (?:выйти|приступить)", re.I)),
    ("start_date", re.compile(r"\bstart\b.*\bdate\b", re.I)),
    ("reason_for_leaving", re.compile(r"reason for leaving|why (?:are you|do you want to) leav|причин\w+ ухода", re.I)),
    ("references", re.compile(r"\breferences?\b|referee|рекомендаци", re.I)),
    ("relocation", re.compile(r"relocat|переезд", re.I)),
]
_PROFILE: list[tuple[str, re.Pattern]] = [
    ("first_name", re.compile(r"first name|given name|^name \(first\)|^имя$", re.I)),
    ("last_name", re.compile(r"last name|surname|family name|^фамилия", re.I)),
    ("full_name", re.compile(r"^(?:full )?name\b|^фио|^имя и фамилия", re.I)),
    ("email", re.compile(r"e-?mail|почта", re.I)),
    ("phone", re.compile(r"phone|mobile|телефон", re.I)),
    ("linkedin", re.compile(r"linkedin", re.I)),
    ("github", re.compile(r"github", re.I)),
    ("website", re.compile(r"website|portfolio|personal (?:site|url)|сайт", re.I)),
    ("location", re.compile(r"^(?:current )?(?:location|city)|where are you (?:based|located)|город", re.I)),
]
_RESUME = re.compile(r"resume|cv\b|curriculum|резюме", re.I)
_LETTER = re.compile(r"cover letter|motivation|why .*(?:join|interested|company)|сопроводительн", re.I)
_YES = re.compile(r"^(yes|да)\b", re.I)
_NO = re.compile(r"^(no|нет)\b", re.I)


def topic_of(label: str) -> str | None:
    for topic, rx in TOPICS:
        if rx.search(label):
            return topic
    return None


def _profile_value(kind: str, kb: KnowledgeBase) -> str | None:
    p = kb.profile
    first, _, last = p.name.partition(" ")
    return {
        "first_name": first, "last_name": last or None, "full_name": p.name, "email": p.email, "phone": p.phone,
        "linkedin": p.links.get("linkedin"), "github": p.links.get("github"),
        "website": p.links.get("site") or p.links.get("website"), "location": p.location,
    }.get(kind)


def _pick_option(options: list[str], wanted: str) -> str | None:
    """Map an answer onto a select/radio option: exact, yes/no, then containment."""
    if not options:
        return wanted
    low = wanted.strip().lower()
    for o in options:
        if o.strip().lower() == low:
            return o
    if _YES.match(low) or _NO.match(low):
        rx = _YES if _YES.match(low) else _NO
        for o in options:
            if rx.match(o.strip()):
                return o
    for o in options:
        if low and (low in o.lower() or o.lower() in low):
            return o
    return None


def _work_auth_answer(label: str, policy: PolicyCfg, job_country: str | None, kb: KnowledgeBase) -> str | None:
    """Visa questions answered by rule; None when the job's country is unknown (never guess)."""
    if re.search(r"citizenship|nationality|гражданств", label, re.I):
        return kb.profile.citizenship
    need = needs_sponsorship(policy, job_country)
    if need is None:
        return None
    if re.search(r"(?:require|need|will you need|now or in the future).*sponsor|sponsorship.*(?:require|need)|"
                 r"нужн\w+ .*виз|потребуется .*виз", label, re.I):
        return "Yes" if need else "No"
    if re.search(r"authori[sz]ed|eligib|right to work|legally|permitted|разрешени", label, re.I):
        return "No" if need else "Yes"
    return None


def resolve(fields: list[FormField], kb: KnowledgeBase, policy: PolicyCfg, *, job_country: str | None,
            lang: str = "en", cv_path: str | None = None, letter: str | None = None,
            posting_salary: str | None = None) -> Resolution:
    res = Resolution()
    for f in fields:
        label = " ".join(f.label.split())
        value, source, topic = None, None, None

        if f.kind == "file":
            if _RESUME.search(label) and cv_path:
                value, source = cv_path, "cv"
            elif _LETTER.search(label) and letter:
                value, source = letter, "letter"   # worker uploads it as a text file
        elif _LETTER.search(label) and f.kind == "textarea" and letter:
            value, source = letter, "letter"
        else:
            topic = topic_of(label)
            if topic:
                res.topics_asked.add(topic)
                mode = policy.sensitive_answers.get(topic, policy.sensitive_answers.get("default", "preapproved"))
                if topic not in policy.sensitive_topics:
                    mode = "preapproved"
                if mode == "work_authorization":
                    value, source = _work_auth_answer(label, policy, job_country, kb), "policy"
                elif mode == "posting_range" and posting_salary:
                    value, source = posting_salary, "posting"
                elif mode == "preapproved":
                    a = kb.answer_for(label)
                    if a:
                        value = a.answer_ru if lang == "ru" and a.answer_ru else a.answer
                        source = "kb_answer"
                if value is not None:
                    res.topics_preapproved.add(topic)
            else:
                for kind, rx in _PROFILE:
                    if rx.search(label):
                        value, source = _profile_value(kind, kb), "profile"
                        break
                if value is None:
                    a = kb.answer_for(label)
                    if a:
                        value = a.answer_ru if lang == "ru" and a.answer_ru else a.answer
                        source = "kb_answer"

        if value is not None and f.kind in ("select", "radio", "checkbox"):
            value = _pick_option(f.options, value)
        if value is None or source is None:
            if f.required or topic:
                res.unanswered.append(f)
            continue
        res.answers.append(FieldAnswer(key=f.key, value=value, source=source, topic=topic))
    return res
