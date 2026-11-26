"""Time helpers. All stored timestamps are UTC ISO-8601 strings in one fixed format, so they
compare correctly as text (ADR 0002)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol

_FMT = "%Y-%m-%dT%H:%M:%S.%fZ"


def iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise ValueError("naive datetime")
    return dt.astimezone(UTC).strftime(_FMT)


def parse_iso(s: str) -> datetime:
    return datetime.strptime(s, _FMT).replace(tzinfo=UTC)


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FakeClock:
    """Deterministic clock for tests."""

    def __init__(self, start: datetime | None = None):
        self._now = start or datetime(2026, 1, 1, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)

    def set(self, when: datetime) -> None:
        self._now = when
