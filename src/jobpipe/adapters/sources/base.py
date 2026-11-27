"""Source interface. A source is made of units (one board per company, one channel, ...); a failing
unit makes the source `partial`, all units failing makes it `blocked` or `error`."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Protocol

from ...domain.job import Posting, SourceKind
from ..http import Blocked, FetchError, Http


@dataclass
class SourceContext:
    params: dict[str, Any] = field(default_factory=dict)
    companies: list[Any] = field(default_factory=list)  # CompanyCfg rows for this ATS
    cursors: dict[str, str] = field(default_factory=dict)  # read and updated by the source
    # Called with each unit's postings as soon as the unit is done (the collector stores them), so a
    # source with 100+ boards never holds every posting in memory at once (439M peak on the VM).
    sink: Callable[[list[Posting]], None] | None = None


@dataclass
class FetchOutcome:
    postings: list[Posting] = field(default_factory=list)
    units_ok: int = 0
    blocked: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    # unit -> every job id currently on that board; only for complete boards (closures)
    complete_ids: dict[str, set[str]] = field(default_factory=dict)
    sink: Callable[[list[Posting]], None] | None = None
    streamed: int = 0                     # postings already handed to the sink

    @property
    def fetched(self) -> int:
        return self.streamed + len(self.postings)

    @property
    def status(self) -> str:
        bad = len(self.blocked) + len(self.errors)
        if bad == 0:
            return "ok"
        if self.units_ok:
            return "partial"
        return "blocked" if self.blocked and not self.errors else "error"

    def run_unit(self, unit: str, fn: Callable[[], None]) -> None:
        start = len(self.postings)
        try:
            fn()
            self.units_ok += 1
        except Blocked as e:
            self.blocked.append(f"{unit}: {e}")
        except FetchError as e:
            self.errors.append(f"{unit}: {e}")
        except Exception as e:  # a parser bug must not stop the other units
            self.errors.append(f"{unit}: {type(e).__name__}: {str(e)[:200]}")
        for p in self.postings[start:]:
            p.description = trim_description(p.description)
        if self.sink is not None and len(self.postings) > start:
            batch = self.postings[start:]
            del self.postings[start:]
            self.streamed += len(batch)
            self.sink(batch)


HEAD_CHARS, TAIL_CHARS = 8000, 4000


def trim_description(text: str) -> str:
    """Keep the start and the end of a long description: visa and remote statements often sit in
    the closing benefits section, so the tail is kept for the signals."""
    if not text or len(text) <= HEAD_CHARS + TAIL_CHARS:
        return text
    return text[:HEAD_CHARS] + "\n…\n" + text[-TAIL_CHARS:]


class Source(Protocol):
    name: str
    kind: SourceKind

    def fetch(self, http: Http, ctx: SourceContext) -> FetchOutcome: ...


def iso_utc(value: Any) -> str | None:
    """ISO-8601, RFC 822, unix seconds or milliseconds -> ISO-8601 UTC string; None if unparsable."""
    if value in (None, ""):
        return None
    try:
        if isinstance(value, int | float):
            ts = value / 1000 if value > 10**11 else value
            dt = datetime.fromtimestamp(ts, tz=UTC)
        else:
            s = str(value).strip()
            try:
                dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            except ValueError:
                try:
                    dt = parsedate_to_datetime(s)
                except (TypeError, ValueError):
                    dt = datetime.strptime(s, "%B %d, %Y")
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    except (ValueError, TypeError, OverflowError, OSError):
        return None
