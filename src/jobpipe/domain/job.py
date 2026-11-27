"""A normalized vacancy as every source returns it. Unknown values are None, never guessed."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

SourceKind = Literal["ats", "aggregator", "community"]
KIND_RANK = {"ats": 0, "aggregator": 1, "community": 2}  # dedupe: lower wins


@dataclass
class Posting:
    source: str
    kind: SourceKind
    source_job_id: str
    company: str
    title: str
    url: str
    description: str = ""
    location: str | None = None
    country: str | None = None  # ISO 3166-1 alpha-2 when the source says so
    remote: str | None = None  # source's own remote label/text
    posted_at: str | None = None  # ISO-8601 UTC
    salary_text: str | None = None
    extra: dict = field(default_factory=dict)

    @property
    def id(self) -> str:
        return f"{self.source}:{self.source_job_id}"
