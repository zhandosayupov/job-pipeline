"""Source registry. Add a source: a class with `name`, `kind` and `fetch(http, ctx)`, a fixture
recorded from a live response in tests/fixtures/sources/, a contract test, then enable it in
sources.yaml."""

from __future__ import annotations

from .aggregators import Amazon, Arbeitnow, Himalayas, Jobicy, WeWorkRemotely
from .ats import Ashby, Greenhouse, Lever
from .base import FetchOutcome, Source, SourceContext
from .community import HackerNews, TelegramChannels

SOURCES: dict[str, type] = {
    cls.name: cls
    for cls in (
        Greenhouse,
        Ashby,
        Lever,
        Amazon,
        Himalayas,
        Jobicy,
        WeWorkRemotely,
        Arbeitnow,
        HackerNews,
        TelegramChannels,
    )
}
ATS_SOURCES = {"greenhouse", "ashby", "lever"}  # driven by companies.yaml

__all__ = ["ATS_SOURCES", "SOURCES", "FetchOutcome", "Source", "SourceContext"]
