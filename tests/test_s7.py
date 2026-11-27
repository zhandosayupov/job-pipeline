"""S7: interview slots from iCal, invitations and reminders, WhatsApp via the bot, filling
placeholders from Telegram, kb extract checks, the daily LLM ping."""
from datetime import UTC, datetime

import httpx
import pytest

from jobpipe.adapters.inbox.mail import parse_email
from jobpipe.adapters.notifier.telegram import RecordingNotifier
from jobpipe.adapters.storage.db import open_db
from jobpipe.config.settings import PolicyCfg, Secrets
from jobpipe.services import calendar as cal
from jobpipe.services import inbox as ib
from jobpipe.services import kb_extract as kx
from jobpipe.services import reply as rp
from jobpipe.services import selfcheck
from jobpipe.services.llm_router import LLMUnavailable
from jobpipe.services.queue import Queue
from jobpipe.workers.handlers import HANDLERS
from jobpipe.workers.runner import Runner

from .conftest import REPO
from .test_inbox_flow import KB, FakeMailbox, add_app, run

MAIL = REPO / "tests" / "fixtures" / "mail"
ICS = (MAIL / "busy.ics").read_text(encoding="utf-8")
NOW = datetime(2026, 11, 26, 6, 0, tzinfo=UTC)          # Thu 11:00 Almaty, the conftest clock


def test_parse_ics_handles_utc_tzid_all_day_and_cancelled():
    ev = {e.uid: e for e in cal.parse_ics(ICS)}
    assert set(ev) == {"busy-1", "busy-2", "busy-3"}
    assert ev["busy-2"].start == datetime(2026, 11, 30, 5, 0, tzinfo=UTC)        # 10:00 Almaty
    assert ev["busy-3"].start == datetime(2026, 11, 30, 19, 0, tzinfo=UTC)       # local midnight


def test_free_slots_skip_busy_days_weekends_and_lead_time():
    slots = cal.free_slots(cal.parse_ics(ICS), NOW)
    # Fri 27: busy 09-14 Almaty -> 14:00; Mon 30 busy all day; Tue 1 holiday; Wed 2 and Thu 3: 10:00
    assert slots == [datetime(2026, 11, 27, 9, 0, tzinfo=UTC), datetime(2026, 12, 2, 5, 0, tzinfo=UTC),
                     datetime(2026, 12, 3, 5, 0, tzinfo=UTC)]
    lines = cal.format_slots(slots[:1], own_tz="Asia/Almaty", their_country="DE", lang="en")
    assert lines == ["Fri 27 Nov 10:00 (Berlin) / 14:00–15:00 Almaty"]
    assert cal.format_slots(slots[:1], own_tz="Asia/Almaty", their_country=None, lang="ru") == [
        "Fri 27 Nov 14:00–15:00 Алматы (UTC+05)"]


def test_booking_links_and_load_busy_with_mock_transport():
    assert cal.booking_links("Pick a time: https://calendly.com/acme/30min.") == ["https://calendly.com/acme/30min"]
    cal.TRANSPORT = httpx.MockTransport(lambda req: httpx.Response(200, text=ICS))
    cal._cache.clear()
    try:
        assert len(cal.load_busy("https://cal.example/secret.ics", "Asia/Almaty")) == 3
        cal.TRANSPORT = httpx.MockTransport(lambda req: httpx.Response(404))
        cal._cache.clear()
        assert cal.load_busy("https://cal.example/secret.ics", "Asia/Almaty") is None   # placeholder stays
        assert cal.load_busy(None, "Asia/Almaty") is None
    finally:
        cal.TRANSPORT = None
        cal._cache.clear()


def test_reply_uses_calendar_slots_instead_of_the_placeholder():
    inv = parse_email((MAIL / "invite_ru.eml").read_bytes())
    d = rp.build(KB, PolicyCfg(), None, message=inv.text, subject=inv.subject, to_name="Мария", category="invite",
                 questions=[], topics=set(), company="Gamma", job_title="Backend Developer", job_country="KZ",
                 slots=["Пт 27 ноя 14:00–15:00 Алматы"])
    assert rp.SLOT_HOLE not in d.text and d.placeholders == [] and d.ok
    assert "Мне удобно в любое из этих окон:\n- Пт 27 ноя 14:00–15:00 Алматы" in d.text


@pytest.fixture
def env(app_cfg, clock):
    conn = open_db(app_cfg.paths.db_path, clock)
    app, _ = add_app(conn, clock, "Acme", "C++ Engineer")
    yield conn, app, FakeMailbox(), RecordingNotifier(), Queue(conn, clock, app_cfg.settings.queue)
    conn.close()


def test_calendar_invite_records_interview_and_reminds_once(env, app_cfg, clock):
    conn, app, mb, n, q = env
    mb.add("inbox", "invite_ics.eml", "T1")
    run(conn, clock, app_cfg, mb, n, q)
    assert conn.execute("SELECT state FROM applications WHERE id = ?", (app,)).fetchone()[0] == "interview"
    assert any("📅" in m and "14:00 (Алматы)" in m and "meet.google.com" in m for m in n.messages)
    clock.advance(4 * 86400 + 4 * 3600)                   # Nov 30 10:00 UTC: 23 h before
    assert ib.remind(conn, clock, app_cfg, n) == 1 and "завтра" in n.messages[-1]
    assert ib.remind(conn, clock, app_cfg, n) == 0
    clock.advance(22 * 3600 + 15 * 60)                    # 45 min before
    assert ib.remind(conn, clock, app_cfg, n) == 1 and "через час" in n.messages[-1]
    assert "C++ Engineer" in n.messages[-1]
    clock.advance(2 * 3600)
    assert ib.remind(conn, clock, app_cfg, n) == 0


def test_invite_draft_carries_slots_and_booking_link(env, app_cfg, clock):
    conn, _app, mb, n, q = env
    app_cfg.secrets = Secrets({"CALENDAR_ICS_URL": "https://cal.example/s.ics"})
    cal.TRANSPORT = httpx.MockTransport(lambda req: httpx.Response(200, text=ICS))
    cal._cache.clear()
    try:
        m = (b"From: Jane <jane@acme.example>\r\nTo: alex@example.com\r\nSubject: Acme C++ Engineer\r\n"
             b"Message-ID: <i2@acme.example>\r\nDate: Thu, 26 Nov 2026 06:00:00 +0000\r\n\r\n"
             b"We would like to invite you to an interview. Or book here: https://calendly.com/acme/45\r\n")
        mb.add("inbox", m, "T2")
        run(conn, clock, app_cfg, mb, n, q)
    finally:
        cal.TRANSPORT = None
        cal._cache.clear()
    card = next(x for x, b in zip(n.messages, n.buttons, strict=True) if b)
    assert "I am available at any of these times:" in card and "(Berlin)" in card
    assert "📅 Ссылка для записи" in card and "calendly.com/acme/45" in card
    assert rp.SLOT_HOLE not in card


def test_whatsapp_via_bot_to_wa_me_link(env, app_cfg, clock):
    conn, app, _mb, n, q = env
    assert "Формат" in ib.whatsapp_inbound(conn, clock, app_cfg, q, "+7 701 123 45 67")
    raw = "+7 701 123 45 67 Olga\nHi! Acme here about the C++ Engineer role. What is your notice period?"
    assert "Принял" in ib.whatsapp_inbound(conn, clock, app_cfg, q, raw)
    assert "уже было" in ib.whatsapp_inbound(conn, clock, app_cfg, q, raw)
    Runner(conn, clock, app_cfg, HANDLERS, n, services={"kb": lambda: KB, "router": lambda: None,
                                                       "mailbox": None}).run(max_seconds=5)
    conv = conn.execute("SELECT rowid, id, application_id, channel FROM conversations").fetchone()
    assert conv["channel"] == "whatsapp" and conv["application_id"] == app      # linked by company name
    buttons = next(b for b in n.buttons if b)
    assert buttons[0][0][0] == "💬 Ссылка для WhatsApp"
    out = ib.message_decision(conn, clock, q, "ok", conv["rowid"])
    assert "https://wa.me/77011234567?text=" in out and "Two%20weeks" in out
    assert conn.execute("SELECT state FROM conversations").fetchone()[0] == "waiting"
    assert conn.execute("SELECT state FROM applications WHERE id = ?", (app,)).fetchone()[0] == "screening"


def test_fill_placeholder_from_telegram(env, app_cfg, clock):
    conn, _app, mb, n, q = env
    mb.add("inbox", "screening.eml", "T3")
    run(conn, clock, app_cfg, mb, n, q)
    conv = conn.execute("SELECT rowid, id FROM conversations").fetchone()
    buttons = next(b for b in n.buttons if b)
    assert any(d == f"m:fill:{conv['rowid']}" for row in buttons for _, d in row)
    values = {"зарплатные ожидания": "5000 EUR gross per month.", "удобное время": "Tuesday afternoon works."}
    text = ""
    while "Всё заполнено" not in text:
        prompt = ib.message_decision(conn, clock, q, "fill", conv["rowid"])
        assert ib.fill_pending(conn, clock)
        value = next(v for k, v in values.items() if k in prompt)
        text, buttons = ib.fill_next(conn, clock, value)
    assert "5000 EUR gross per month." in text and not ib.fill_pending(conn, clock)
    assert not any(d.startswith("m:fill") for row in buttons for _, d in row)
    ib.message_decision(conn, clock, q, "ok", conv["rowid"])
    Runner(conn, clock, app_cfg, HANDLERS, n, services={"kb": lambda: KB, "router": lambda: None,
                                                       "mailbox": lambda: mb}).run(max_seconds=5)
    assert b"5000 EUR gross per month." in mb.drafts[0]


CV = """Alex Example
Software Engineer (C++), SearchCo, 2023-07 - present
- Cut p99 latency of the ranking service by 35% with a new allocator (C++, Linux)
- Built an incremental indexer for 2 billion documents per day (Kafka)
"""


def test_kb_extract_keeps_only_quoted_facts(tmp_path):
    q = "Cut p99 latency of the ranking service by 35% with a new allocator (C++, Linux)"
    out = kx.Extracted.model_validate({
        "roles": [{"id": "searchco", "employer": "SearchCo", "title": "Software Engineer (C++)", "start": "2023-07"},
                  {"id": "ghost", "employer": "Google", "title": "SWE", "start": "2020-01"}],
        "facts": [
            {"id": "lat", "role": "searchco", "statement": "Cut p99 ranking latency by 35%", "tech": ["C++"],
             "quote": q},
            {"id": "lat2", "role": "searchco", "statement": "Cut p99 ranking latency by 50%", "quote": q},
            {"id": "rust", "statement": "Wrote Rust services", "tech": ["Rust"], "quote": "Built an incremental"},
            {"id": "inv", "statement": "Led a team of 10", "quote": "Led a team of 10 engineers"},
        ]})
    res = kx.check(out, CV)
    assert [r.id for r in res.roles] == ["searchco"] and [f.id for f in res.facts] == ["lat"]
    assert len(res.dropped) == 4 and any("50" in d for d in res.dropped)
    path = kx.write_draft(res, tmp_path)
    text = path.read_text(encoding="utf-8")
    assert path.name == "facts.draft.yaml" and "Cut p99 ranking latency by 35%" in text and "# DRAFT" in text


def test_daily_llm_ping_runs_once_a_day(app_cfg, clock):
    conn = open_db(app_cfg.paths.db_path, clock)
    calls = []

    class Down:
        def complete(self, task, variables, schema=None, personal=False):
            calls.append(task)
            raise LLMUnavailable(task, [])

    assert not selfcheck.llm_check(conn, clock, lambda: Down()).ok and calls == ["ping"]
    assert not selfcheck.llm_check(conn, clock, lambda: Down()).ok and calls == ["ping"]   # cached for the day
    clock.advance(86400)

    class Up:
        def complete(self, task, variables, schema=None, personal=False):
            calls.append(task)

    assert selfcheck.llm_check(conn, clock, lambda: Up()).ok and len(calls) == 2


def test_slots_prefer_the_recruiters_working_hours():
    berlin = cal.free_slots(cal.parse_ics(ICS), NOW, their_tz="Europe/Berlin")
    from zoneinfo import ZoneInfo

    assert all(9 <= s.astimezone(ZoneInfo("Europe/Berlin")).hour <= 17 for s in berlin)
    assert berlin[1] == datetime(2026, 12, 2, 8, 0, tzinfo=UTC)          # 13:00 Almaty = 09:00 Berlin


def test_fill_request_expires(env, app_cfg, clock):
    conn, _app, mb, n, q = env
    mb.add("inbox", "screening.eml", "T3")
    run(conn, clock, app_cfg, mb, n, q)
    rowid = conn.execute("SELECT rowid FROM conversations").fetchone()[0]
    ib.message_decision(conn, clock, q, "fill", rowid)
    assert ib.fill_pending(conn, clock)
    clock.advance(11 * 60)
    assert not ib.fill_pending(conn, clock)
    assert "истёк" in ib.fill_next(conn, clock, "late text")[0]


def test_overlapping_reminder_runs_send_once(env, app_cfg, clock):
    conn, _app, mb, n, q = env
    mb.add("inbox", "invite_ics.eml", "T1")
    run(conn, clock, app_cfg, mb, n, q)
    clock.advance(4 * 86400 + 4 * 3600)
    conv = conn.execute("SELECT id, data_json FROM conversations").fetchone()
    stale = conv["data_json"]                                        # a second run read the row before the first
    assert ib.remind(conn, clock, app_cfg, n) == 1
    conn.execute("UPDATE conversations SET data_json = ? WHERE id = ?", (stale, conv["id"]))
    assert ib.remind(conn, clock, app_cfg, n) == 0                   # the key holds
