"""Deterministic prefilter (no LLM): drop what is clearly out of scope, keep the rest for labeling.

Rules, in order; the first that fires drops the job and is recorded with the matched keyword:
  title_not_eng      the title has no engineering keyword
  title_excluded     intern / staff / principal / manager / ... (with exceptions)
  text_excluded      security clearance, citizens only, RU citizenship ...
  location_ru_by     onsite in Russia or Belarus (no relocation there)
  no_sponsorship     the posting refuses sponsorship and the job is neither remote nor in a country
                     the owner may work in without a visa
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache

from ..config.settings import FiltersCfg
from ..domain.job import Posting
from . import text as T


@dataclass(frozen=True)
class Decision:
    keep: bool
    rule: str | None = None
    matched: str | None = None
    signals: dict = field(default_factory=dict)


@lru_cache(maxsize=512)
def _compile(entry: str) -> re.Pattern:
    if entry.startswith("re:"):
        return re.compile(entry[3:], re.I)
    # Whole word, but "c++" / "c#" style entries keep their symbols.
    return re.compile(rf"(?<![\w+#]){re.escape(entry)}(?![\w+#])", re.I)


def first_match(entries: list[str], text: str) -> str | None:
    for e in entries:
        if _compile(e).search(text or ""):
            return e
    return None


def signals(p: Posting) -> dict:
    blob = "\n".join(filter(None, [p.title, p.location, p.remote, p.description]))
    visa, visa_ev = T.visa_signal(blob)
    remote = T.remote_signal("\n".join(filter(None, [p.remote, p.location, p.title]))) or T.remote_signal(p.description)
    return {
        "visa": visa,
        "visa_evidence": visa_ev,
        "remote": remote,
        "salary": p.salary_text or T.salary_text(p.description),
        "lang": T.detect_lang(f"{p.title}\n{p.description}"),
        "excerpt": T.excerpt(p.description),
    }


def decide(p: Posting, f: FiltersCfg, work_authorized: list[str]) -> Decision:
    sig = signals(p)
    title = p.title or ""
    if f.title_include and not first_match(f.title_include, title):
        return Decision(False, "title_not_eng", None, sig)
    hit = first_match(f.title_exclude, title)
    if hit and not first_match(f.title_exclude_exceptions, title):
        return Decision(False, "title_excluded", hit, sig)
    hit = first_match(f.text_exclude, f"{title}\n{p.description}")
    if hit:
        return Decision(False, "text_excluded", hit, sig)
    hit = first_match(f.onsite_excluded_locations, f"{p.location or ''}\n{p.country or ''}")
    if hit and sig["remote"] != "remote":
        return Decision(False, "location_ru_by", hit, sig)
    authorized = {c.upper() for c in work_authorized}
    if sig["visa"] == "no" and sig["remote"] != "remote" and (p.country or "").upper() not in authorized:
        return Decision(False, "no_sponsorship", sig["visa_evidence"], sig)
    return Decision(True, None, None, sig)
