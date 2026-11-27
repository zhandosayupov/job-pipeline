"""`jobpipe kb extract cv.md` (S7): a first draft of kb/facts.yaml from the owner's CV, for review.

The LLM call is pseudonymized (personal task). Every fact must quote a line that is really in the
CV, and its numbers and technologies must appear in that quote; anything else is dropped and listed.
The result goes to `kb/facts.draft.yaml`: it never overwrites `facts.yaml`, the owner merges it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from .kb import Fact, Role


class ExtractedFact(Fact):
    quote: str = ""


class Extracted(BaseModel):
    roles: list[Role] = Field(default_factory=list)
    facts: list[ExtractedFact] = Field(default_factory=list)


@dataclass
class ExtractResult:
    roles: list[Role] = field(default_factory=list)
    facts: list[Fact] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)


def _norm(s: str) -> str:
    return " ".join(re.sub(r"[^\w%+#.]+", " ", s.lower()).split())


def check(out: Extracted, cv: str) -> ExtractResult:
    res = ExtractResult()
    body = _norm(cv)
    role_ids = set()
    for r in out.roles:
        if _norm(r.employer) in body:
            res.roles.append(r)
            role_ids.add(r.id)
        else:
            res.dropped.append(f"role {r.id}: its employer is not in the CV")   # no employer name in the log
    seen = set()
    for f in out.facts:
        q = _norm(f.quote)
        why = None
        if not q or q not in body:
            why = "quote not found in the CV"
        else:
            nums = re.findall(r"\d+(?:[.,]\d+)?", f.statement)
            missing = [n for n in nums if n not in f.quote]
            tech = [t for t in f.tech if _norm(t) not in body]
            if missing:
                why = f"numbers {missing} not in its quote"
            elif tech:
                why = f"technologies {tech} not in the CV"
        if why:
            res.dropped.append(f"{f.id}: {why}")
            continue
        fid = f.id if f.id not in seen else f"{f.id}-{len(seen)}"
        seen.add(fid)
        data = f.model_dump(exclude={"quote"})
        data.update(id=fid, role=f.role if f.role in role_ids else None, source="kb extract")
        res.facts.append(Fact.model_validate(data))
    return res


def extract(router, cv_text: str, pii: dict[str, list[str]]) -> ExtractResult:
    """The CV is pseudonymized here and the answer is never restored: the fact base needs no name,
    e-mail or phone, so they cannot reach the draft file or the console (CodeQL clear-text storage)."""
    from .pii import Pseudonymizer

    redacted = Pseudonymizer(pii).redact(cv_text)
    out = router.complete("kb_extract", {"cv": redacted[:20000]}, schema=Extracted, personal=True)
    return check(out.data, redacted)


def write_draft(res: ExtractResult, kb_dir: Path) -> Path:
    kb_dir.mkdir(parents=True, exist_ok=True)
    path = kb_dir / "facts.draft.yaml"
    doc = {"roles": [r.model_dump(exclude_none=True) for r in res.roles],
           "facts": [f.model_dump(exclude_defaults=True) | {"id": f.id, "statement": f.statement}
                     for f in res.facts]}
    head = ("# DRAFT from `jobpipe kb extract`. Review every line against your CV, fix wording, then move\n"
            "# what is true into facts.yaml. Dropped by the checks:\n"
            + "".join(f"#   {d}\n" for d in res.dropped))
    path.write_text(head + yaml.safe_dump(doc, allow_unicode=True, sort_keys=False, width=110), encoding="utf-8")
    return path
