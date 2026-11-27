"""Interview scheduling helpers (S7, ADR 0025). Read-only: nothing here books, accepts or creates events.

* busy times come from the calendar's secret iCal address (`CALENDAR_ICS_URL` in secrets.env;
  Google Calendar -> Settings -> "Secret address in iCal format"), so no OAuth is needed;
* free slots are proposed in the owner's working hours and shown in the recruiter's time zone
  when the job's country gives one; the owner approves the draft that contains them;
* an invitation (a text/calendar part or an .ics attachment) is parsed for its start, end, title
  and meeting link, so the interview can be recorded and reminded.

The iCal parser is deliberately small: VEVENT DTSTART/DTEND in UTC, with TZID, or all-day.
Recurring events (RRULE) are taken at their first occurrence only (listed in docs/TODO.md).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# One representative zone per country, for showing slots to the recruiter. Unknown: Almaty + UTC.
COUNTRY_TZ = {"DE": "Europe/Berlin", "NL": "Europe/Amsterdam", "GB": "Europe/London", "UK": "Europe/London",
              "IE": "Europe/Dublin", "FR": "Europe/Paris", "ES": "Europe/Madrid", "IT": "Europe/Rome",
              "PL": "Europe/Warsaw", "CH": "Europe/Zurich", "AT": "Europe/Vienna", "SE": "Europe/Stockholm",
              "CY": "Asia/Nicosia", "AE": "Asia/Dubai", "SG": "Asia/Singapore", "HK": "Asia/Hong_Kong",
              "US": "America/New_York", "CA": "America/Toronto", "KZ": "Asia/Almaty", "RU": "Europe/Moscow",
              "AM": "Asia/Yerevan", "GE": "Asia/Tbilisi", "RS": "Europe/Belgrade", "PT": "Europe/Lisbon",
              "CZ": "Europe/Prague", "IL": "Asia/Jerusalem", "JP": "Asia/Tokyo", "AU": "Australia/Sydney"}
MEETING_LINK = re.compile(r"https?://(?:[\w-]+\.)?(?:zoom\.us|meet\.google\.com|teams\.microsoft\.com|"
                          r"teams\.live\.com|webex\.com|whereby\.com)/\S+", re.I)
BOOKING_LINK = re.compile(r"https?://(?:[\w-]+\.)?(?:calendly\.com|cal\.com|goodtime\.io|savvycal\.com|"
                          r"youcanbook\.me|hubspot\.com/meetings|zcal\.co)/\S+", re.I)


@dataclass(frozen=True)
class Event:
    start: datetime          # aware, UTC
    end: datetime
    summary: str = ""
    location: str = ""
    url: str = ""
    uid: str = ""


def _unfold(text: str) -> list[str]:
    return re.sub(r"\r?\n[ \t]", "", text).splitlines()


def _dt(value: str, params: str, default_tz: str) -> datetime:
    if re.fullmatch(r"\d{8}", value):                                  # all-day
        d = datetime.strptime(value, "%Y%m%d").date()
        return datetime.combine(d, time(0), ZoneInfo(default_tz)).astimezone(UTC)
    if value.endswith("Z"):
        return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    m = re.search(r"TZID=([^;:]+)", params)
    try:
        tz = ZoneInfo(m.group(1).strip('"')) if m else ZoneInfo(default_tz)
    except ZoneInfoNotFoundError:
        tz = ZoneInfo(default_tz)
    return datetime.strptime(value[:15], "%Y%m%dT%H%M%S").replace(tzinfo=tz).astimezone(UTC)


def _text(v: str) -> str:
    return v.replace("\\n", "\n").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\").strip()


def parse_ics(text: str, default_tz: str = "Asia/Almaty") -> list[Event]:
    events, cur = [], None
    for line in _unfold(text):
        if line == "BEGIN:VEVENT":
            cur = {}
        elif line == "END:VEVENT" and cur is not None:
            if "DTSTART" in cur and cur.get("STATUS", ("", ""))[0].upper() != "CANCELLED":
                start = _dt(*cur["DTSTART"], default_tz)
                end = _dt(*cur["DTEND"], default_tz) if "DTEND" in cur else start + timedelta(hours=1)
                desc = cur.get("DESCRIPTION", ("", ""))[0]
                loc = _text(cur.get("LOCATION", ("", ""))[0])
                link = (MEETING_LINK.search(loc) or MEETING_LINK.search(_text(desc)))
                events.append(Event(start, end, _text(cur.get("SUMMARY", ("", ""))[0]), loc,
                                    link.group(0) if link else "", cur.get("UID", ("", ""))[0]))
            cur = None
        elif cur is not None and ":" in line:
            head, value = line.split(":", 1)
            name, _, params = head.partition(";")
            if name in ("DTSTART", "DTEND"):
                cur[name] = (value.strip(), params)
            elif name in ("SUMMARY", "LOCATION", "DESCRIPTION", "UID", "STATUS"):
                cur[name] = (value, params)
    return events


def free_slots(busy: list[Event], now: datetime, *, tz: str = "Asia/Almaty", days: int = 5, start_h: int = 10,
               end_h: int = 19, length_min: int = 60, count: int = 3, lead_h: int = 18,
               their_tz: str | None = None) -> list[datetime]:
    """Up to `count` free slots on working days, at most one per day, not before now + lead_h.
    With `their_tz`, hours that fall in 09:00-18:00 for the recruiter come first (S7 review: 10:00 in
    Almaty is 06:00 in Berlin); a day with no such hour falls back to the owner's first free hour."""
    zone = ZoneInfo(tz)
    theirs = ZoneInfo(their_tz) if their_tz and their_tz != tz else None
    out: list[datetime] = []
    earliest = now + timedelta(hours=lead_h)
    day: date = now.astimezone(zone).date()
    checked = 0
    while len(out) < count and checked < days + 9:
        day += timedelta(days=1)
        if day.weekday() >= 5:
            continue
        checked += 1
        free = []
        for h in range(start_h, end_h):
            s = datetime.combine(day, time(h), zone).astimezone(UTC)
            e = s + timedelta(minutes=length_min)
            if s < earliest or e > datetime.combine(day, time(end_h), zone).astimezone(UTC):
                continue
            if all(e <= b.start or s >= b.end for b in busy):
                free.append(s)
        if free:
            good = [s for s in free if theirs and s.astimezone(theirs).hour >= 9
                    and (s + timedelta(minutes=length_min)).astimezone(theirs).hour <= 18]
            out.append((good or free)[0])
    return out


def format_slots(slots: list[datetime], *, own_tz: str, their_country: str | None, lang: str,
                 length_min: int = 60) -> list[str]:
    their = COUNTRY_TZ.get((their_country or "").upper())
    lines = []
    for s in slots:
        mine = s.astimezone(ZoneInfo(own_tz))
        end = mine + timedelta(minutes=length_min)
        own = f"{mine:%H:%M}–{end:%H:%M} {'Алматы' if lang == 'ru' else 'Almaty'}"
        if their and their != own_tz:
            t = s.astimezone(ZoneInfo(their))
            label = f"{t:%a %d %b %H:%M} ({their.split('/')[-1].replace('_', ' ')}) / {own}"
        else:
            off = int(mine.utcoffset().total_seconds() // 60)
            sign, off = ("+" if off >= 0 else "-"), abs(off)
            label = f"{mine:%a %d %b} {own} (UTC{sign}{off // 60:02d}" + (f":{off % 60:02d})" if off % 60 else ")")
        lines.append(label)
    return lines


def booking_links(text: str, links: list[tuple[str, str]] = ()) -> list[str]:
    found = [m.group(0).rstrip(".,)") for m in BOOKING_LINK.finditer(text or "")]
    found += [h for h, _ in links if BOOKING_LINK.match(h)]
    return list(dict.fromkeys(found))


# --- loading the owner's busy times ----------------------------------------------------------

TRANSPORT = None          # tests set an httpx.MockTransport here
_cache: dict[str, tuple[float, list[Event]]] = {}


def load_busy(url: str | None, tz: str, *, ttl_s: float = 600) -> list[Event] | None:
    """Busy events from the secret iCal address; None when not configured or not reachable
    (then the draft keeps its placeholder: never propose times we could not check)."""
    import time as _time

    import httpx

    if not url:
        return None
    hit = _cache.get(url)
    if hit and _time.monotonic() - hit[0] < ttl_s:
        return hit[1]
    try:
        with httpx.Client(timeout=20, transport=TRANSPORT, follow_redirects=True) as c:
            r = c.get(url)
            r.raise_for_status()
    except httpx.HTTPError:
        return None
    events = parse_ics(r.text, tz)
    _cache[url] = (_time.monotonic(), events)
    return events


def slot_lines(url: str | None, now: datetime, *, tz: str, country: str | None, lang: str) -> list[str] | None:
    busy = load_busy(url, tz)
    if busy is None:
        return None
    slots = free_slots(busy, now, tz=tz, their_tz=COUNTRY_TZ.get((country or "").upper()))
    return format_slots(slots, own_tz=tz, their_country=country, lang=lang) or None
