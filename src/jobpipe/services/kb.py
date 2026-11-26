"""Knowledge base (spec §8): the only source of truth for anything sent on the owner's behalf.

Lives in $JOBPIPE_HOME/kb/ (never in the repo):
  profile.yaml   name, contacts, links, languages, education: personal values, pseudonymized before LLMs
  facts.yaml     every claim the CV and letters may make, with allowed phrasings and the me/team boundary
  answers.yaml   the owner's verbatim answers to recurring form questions (ADR 0016)

v1 loads everything (it is small). `retrieve()` keeps the port a RAG adapter could replace later.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Role(_M):
    """A job held: employer, title, dates. Bullets point at facts."""

    id: str
    employer: str
    title: str
    start: str                    # YYYY-MM
    end: str | None = None        # YYYY-MM, None = present
    location: str | None = None


class Fact(_M):
    id: str
    statement: str                                   # the canonical wording (English)
    phrasings: list[str] = Field(default_factory=list)  # other allowed wordings, same claim
    role: str | None = None                          # Role.id it belongs to; None = general
    kind: Literal["achievement", "responsibility", "skill", "education", "award", "project", "other"] = "achievement"
    contribution: Literal["me", "team", "led"] = "me"  # what the owner may claim as theirs
    tech: list[str] = Field(default_factory=list)    # technologies this fact proves
    tags: list[str] = Field(default_factory=list)    # hft, search, backend, ... (variant selection)
    source: str | None = None                        # file it came from, for review

    def wordings(self) -> list[str]:
        return [self.statement, *self.phrasings]


class Answer(_M):
    topic: str                     # compensation, notice_period, start_date, visa, ...
    question_patterns: list[str] = Field(default_factory=list)   # regexes matched against form labels
    answer: str
    answer_ru: str | None = None


class Profile(_M):
    name: str
    headline: str = ""
    email: str | None = None
    phone: str | None = None
    location: str | None = None
    links: dict[str, str] = Field(default_factory=dict)       # github, linkedin, site
    citizenship: str | None = None
    languages: list[str] = Field(default_factory=list)
    education: list[str] = Field(default_factory=list)        # rendered as given
    skills: dict[str, list[str]] = Field(default_factory=dict)  # group -> items, all must be backed by facts


class KnowledgeBase(_M):
    profile: Profile
    roles: list[Role] = Field(default_factory=list)
    facts: list[Fact] = Field(default_factory=list)
    answers: list[Answer] = Field(default_factory=list)

    @model_validator(mode="after")
    def _consistent(self) -> KnowledgeBase:
        ids = [f.id for f in self.facts]
        dup = {i for i in ids if ids.count(i) > 1}
        if dup:
            raise ValueError(f"duplicate fact ids: {sorted(dup)}")
        roles = {r.id for r in self.roles}
        bad = [f.id for f in self.facts if f.role and f.role not in roles]
        if bad:
            raise ValueError(f"facts point at unknown roles: {bad}")
        return self

    # --- lookups -------------------------------------------------------------------------

    def fact(self, fid: str) -> Fact | None:
        return next((f for f in self.facts if f.id == fid), None)

    def role(self, rid: str) -> Role | None:
        return next((r for r in self.roles if r.id == rid), None)

    def employers(self) -> set[str]:
        return {r.employer for r in self.roles}

    def technologies(self) -> set[str]:
        out = {t for f in self.facts for t in f.tech}
        out |= {s for items in self.profile.skills.values() for s in items}
        return out

    def retrieve(self, query: str = "") -> list[Fact]:
        """v1: the whole KB. The port stays so a retrieval adapter can replace it."""
        return list(self.facts)

    def answer_for(self, question: str, lang: str = "en") -> Answer | None:
        for a in self.answers:
            if any(re.search(p, question, re.I) for p in a.question_patterns):
                return a
        return None

    def pii_values(self) -> dict[str, list[str]]:
        """Values to pseudonymize before personal LLM calls (ADR 0008)."""
        p = self.profile
        out: dict[str, list[str]] = {"NAME": [p.name, *p.name.split()[:1]]}
        if p.email:
            out["EMAIL"] = [p.email]
        if p.phone:
            out["PHONE"] = [p.phone]
        for k, v in p.links.items():
            out[f"LINK_{k.upper()}"] = [v]
        return out


def load(kb_dir: Path) -> KnowledgeBase:
    def y(name: str) -> dict:
        f = kb_dir / name
        return (yaml.safe_load(f.read_text(encoding="utf-8")) or {}) if f.exists() else {}

    facts = y("facts.yaml")
    return KnowledgeBase(
        profile=Profile.model_validate(y("profile.yaml")),
        roles=facts.get("roles", []),
        facts=facts.get("facts", []),
        answers=y("answers.yaml").get("answers", []),
    )
