"""`jobpipe run inbox` (S5, ADR 0023): read recruiter mail, link it to applications, move the funnel,
and draft replies for the owner's Approve. Code never sends a message: an approved reply becomes a
Gmail draft (IMAP APPEND) or a Telegram draft (userbot on the laptop) that the owner sends.

  store    only job-related mail is kept (linked thread, hiring-platform sender, job words);
           the rest of the mailbox is skipped and never reaches an LLM
  process  triage (rules, then LLM) -> application stage (replied / screening / interview /
           rejected; an "application received" mail confirms a submit_unknown) -> conversation:
           draft + card for invites, screening questions and requests; waiting otherwise
  outbound the owner's sent replies close the loop: approved -> sent -> waiting, and whether the
           draft went out unedited is recorded (the L2 eligibility count, policy.yaml)
  ghosted  applied with no reply for `inbox.ghosted_after_days`
"""
from __future__ import annotations

import difflib
import hashlib
import json
import logging
import re
import sqlite3
from collections import deque
from dataclasses import dataclass, field
from datetime import timedelta
from urllib.parse import unquote

from ..adapters.inbox.mail import build_reply
from ..clock import Clock, iso
from ..config.settings import AppConfig
from ..domain import states
from ..domain.job import Posting
from ..domain.message import InMessage
from ..ports.notifier import Notifier
from . import calendar as cal
from . import control
from . import reply as replymod
from . import triage as tr
from .events import create_entity, record_event, transition
from .kb import KnowledgeBase
from .llm_router import LLMUnavailable
from .policy import Decision, Route, decide_message
from .queue import Queue
from .text import norm_company

log = logging.getLogger(__name__)
DRAFT, REPLY, TG_DRAFT, PROCESS = "inbox.draft", "inbox.reply", "tg.draft", "inbox.process"
LIVE_APPS = ("applied", "submit_unknown", "replied", "screening", "interview", "offer", "ghosted")
# Application states the inbox may walk through; never back into the apply flow.
_FUNNEL = {"applied", "submit_unknown", "replied", "screening", "interview", "offer", "rejected", "ghosted"}
NO_REPLY = re.compile(r"no-?reply|do-?not-?reply|notifications?@|mailer-daemon|bounce", re.I)
HH_VACANCY = re.compile(r"https?://(?:[\w-]+\.)?hh\.(kz|ru)/vacancy/(\d+)", re.I)


@dataclass
class InboxResult:
    fetched: int = 0
    stored: int = 0
    skipped: int = 0
    processed: int = 0
    categories: dict[str, int] = field(default_factory=dict)
    drafts: int = 0
    postings: int = 0
    ghosted: int = 0
    reminders: int = 0
    errors: int = 0


def owner_addresses(cfg: AppConfig, kb: KnowledgeBase | None) -> set[str]:
    out = {a.lower() for a in (cfg.settings.inbox.gmail_user, kb.profile.email if kb else None) if a}
    return out


def _conv_id(m: InMessage) -> str:
    root = m.thread_key or (m.references[0] if m.references else m.in_reply_to) or m.ext_id
    return f"conv:{m.channel}:{hashlib.sha1(root.encode('utf-8')).hexdigest()[:16]}"


def _find_conversation(conn: sqlite3.Connection, m: InMessage) -> str | None:
    cid = _conv_id(m)
    if conn.execute("SELECT 1 FROM conversations WHERE id = ?", (cid,)).fetchone():
        return cid
    ids = [x for x in (m.in_reply_to, *m.references) if x]
    if m.thread_key:
        row = conn.execute("SELECT conversation_id FROM messages WHERE channel = ? AND thread_key = ? "
                           "AND conversation_id IS NOT NULL LIMIT 1", (m.channel, m.thread_key)).fetchone()
        if row:
            return row[0]
    if ids:
        marks = ",".join("?" for _ in ids)
        row = conn.execute(f"SELECT conversation_id FROM messages WHERE ext_id IN ({marks}) "
                           "AND conversation_id IS NOT NULL LIMIT 1", ids).fetchone()
        if row:
            return row[0]
    return None


def _words(s: str) -> str:
    return " " + " ".join(re.sub(r"[^\w+#]+", " ", (s or "").lower()).split()) + " "   # keeps c++, c#


def link_application(conn: sqlite3.Connection, m: InMessage) -> tuple[str | None, bool]:
    """(application id, ambiguous). A live application whose company name appears in the sender,
    subject or text; several at one company: the one whose title matches best, else the latest."""
    hay = _words(f"{m.from_name} {m.domain.replace('.', ' ')} {m.subject} {m.text[:3000]}")
    domain = m.domain.replace("-", "")
    marks = ",".join("?" for _ in LIVE_APPS)
    rows = conn.execute(
        f"SELECT a.id, a.updated_at, j.company, j.title FROM applications a JOIN jobs j ON j.id = a.job_id "
        f"WHERE a.state IN ({marks})", LIVE_APPS).fetchall()
    cands = []
    for r in rows:
        c = norm_company(r["company"])
        if len(c) < 3:
            continue
        if f" {c} " in hay or (domain and c.replace(" ", "") in domain.split(".")[0]):
            title_hits = sum(1 for w in set(_words(r["title"]).split()) if (len(w) > 3 or "+" in w or "#" in w)
                             and f" {w} " in hay)
            cands.append((title_hits, r["updated_at"], r["id"], c))
    if not cands:
        return None, False
    cands.sort(reverse=True)
    companies = {c[3] for c in cands}
    ambiguous = len(companies) > 1 or (len(cands) > 1 and cands[0][0] == cands[1][0])
    return cands[0][2], ambiguous


# --- store ---------------------------------------------------------------------------------


def store(conn: sqlite3.Connection, clock: Clock, cfg: AppConfig, m: InMessage, *, owner: set[str],
          force: bool = False) -> str | None:
    """Keep a message if it is job-related (or `force`: the Telegram userbot account is job-only).
    Returns the message id, or None when skipped or already stored."""
    if conn.execute("SELECT 1 FROM messages WHERE id = ?", (m.id,)).fetchone():
        return None
    ic = cfg.settings.inbox
    if m.from_addr.lower() in owner:
        m.direction = "out"
    conv = _find_conversation(conn, m)
    if m.direction == "out" and conv is None:
        return None   # the owner's own mail outside a tracked thread is not ours to read
    rule = tr.by_rules(m, alert_senders=ic.alert_senders) if m.direction == "in" else None
    alert = rule is not None and rule.category == "job_alert"   # alerts feed postings, not conversations
    app_id, ambiguous = (None, False)
    if conv is None and not alert:
        app_id, ambiguous = link_application(conn, m)
    if not force and conv is None and not tr.is_job_related(m, ats_domains=ic.ats_domains,
                                                             alert_senders=ic.alert_senders,
                                                             linked=app_id is not None, ignore=ic.ignore_senders):
        return None
    now = iso(clock.now())
    if conv is None and not alert:
        conv = _conv_id(m)
        create_entity(conn, clock, "conversation", conv, actor="inbox",
                      columns={"application_id": app_id, "channel": m.channel},
                      data={"subject": m.subject, "counterpart": m.from_addr, "name": m.from_name,
                            "link_ambiguous": ambiguous})
    data = {"to": m.to, "in_reply_to": m.in_reply_to, "references": m.references, "links": m.links[:200],
            "auto": m.auto, "invites": m.invites[:3]}
    conn.execute(
        "INSERT INTO messages(id, conversation_id, channel, direction, ext_id, thread_key, from_addr, from_name, "
        "subject, ts, body, data_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (m.id, conv, m.channel, m.direction, m.ext_id, m.thread_key, m.from_addr, m.from_name, m.subject, m.ts,
         m.text, json.dumps(data, ensure_ascii=False), now))

    return m.id


def _message(row: sqlite3.Row) -> InMessage:
    d = json.loads(row["data_json"] or "{}")
    return InMessage(channel=row["channel"], ext_id=row["ext_id"], ts=row["ts"], from_addr=row["from_addr"] or "",
                     from_name=row["from_name"] or "", to=d.get("to", []), subject=row["subject"] or "",
                     text=row["body"], links=[tuple(x) for x in d.get("links", [])], in_reply_to=d.get("in_reply_to"),
                     references=d.get("references", []), thread_key=row["thread_key"], direction=row["direction"],
                     auto=d.get("auto", False), invites=d.get("invites", []))


# --- state helpers -------------------------------------------------------------------------


def path_to(table: states.Table, src: str, dst: str, allowed: set[str]) -> list[str] | None:
    """Shortest list of states after `src` that ends in `dst`, staying inside `allowed`."""
    if src == dst:
        return []
    seen, todo = {src}, deque([(src, [])])
    while todo:
        s, p = todo.popleft()
        for n in sorted(table.get(s, ())):
            if n in seen or n not in allowed:
                continue
            if n == dst:
                return [*p, n]
            seen.add(n)
            todo.append((n, [*p, n]))
    return None


def advance_application(conn: sqlite3.Connection, clock: Clock, app_id: str, target: str, *,
                        evidence: str) -> list[str]:
    """Walk an application forward to `target` (replied, screening, interview, rejected, applied).
    Never moves it backwards: an interview stays an interview when a later mail is merely a reply."""
    row = conn.execute("SELECT state FROM applications WHERE id = ?", (app_id,)).fetchone()
    if row is None:
        return []
    order = ["submit_unknown", "applied", "ghosted", "replied", "screening", "interview", "offer"]
    cur = row["state"]
    if target in order and cur in order and order.index(target) <= order.index(cur) and \
            not (cur == "ghosted" and target in ("replied", "screening", "interview")):
        return []
    path = path_to(states.APPLICATION, cur, target, _FUNNEL - ({"ghosted"} if target != "ghosted" else set()))
    if path is None:
        record_event(conn, clock, entity="application", entity_id=app_id, kind="note", actor="inbox",
                     data={"inbox": f"{target} seen while {cur}", "evidence": evidence})
        return []
    for s in path:
        cur = transition(conn, clock, "application", app_id, s, actor="inbox", data={"evidence": evidence})
        if cur == "submit_unknown":
            _confirm_submit(conn, clock, app_id)
    return path


def _confirm_submit(conn, clock, app_id) -> None:
    """A mail from the company proves the application arrived: the unknown submit is resolved as done."""
    key = conn.execute("SELECT idempotency_key FROM applications WHERE id = ?", (app_id,)).fetchone()[0]
    se = control.SideEffects(conn, clock)
    if se.status(key) in ("unknown", "planned"):
        se.resolve(key, "done", actor="inbox")


def _conv(conn, conv_id) -> sqlite3.Row:
    return conn.execute("SELECT rowid, * FROM conversations WHERE id = ?", (conv_id,)).fetchone()


def _walk(conn, clock, conv_id: str, steps: list[str], data: dict | None = None, actor: str = "inbox") -> None:
    for s in steps:
        transition(conn, clock, "conversation", conv_id, s, actor=actor, data=data)


def _reopen(conn, clock, conv_id: str) -> str:
    """A new inbound message: back to `new` (or `triaged` when a draft was pending: it is stale now)."""
    state = _conv(conn, conv_id)["state"]
    if state == "waiting":
        _walk(conn, clock, conv_id, ["new"])
    elif state == "sent":
        _walk(conn, clock, conv_id, ["waiting", "new"])
    elif state == "draft_ready":
        _walk(conn, clock, conv_id, ["triaged"], data={"draft": "superseded by a newer message"})
    return _conv(conn, conv_id)["state"]


def _save_conv(conn, conv_id, data) -> None:
    conn.execute("UPDATE conversations SET data_json = ? WHERE id = ?", (json.dumps(data, ensure_ascii=False),
                                                                        conv_id))



# --- process -------------------------------------------------------------------------------


def _triage(cfg, router, m: InMessage) -> tr.Triage:
    rule = tr.by_rules(m, alert_senders=cfg.settings.inbox.alert_senders)
    if rule:
        return rule
    if router is None:
        return tr.fallback(m)
    try:
        out = router.complete("triage", {"subject": m.subject, "sender": m.from_name or m.domain,
                                         "message": m.text[:4000]}, schema=tr.TriageOut, personal=True)
    except LLMUnavailable:
        return tr.fallback(m)
    return tr.check_llm(out.data, m)


def alert_postings(m: InMessage) -> list[Posting]:
    """hh.kz / hh.ru vacancy links of a saved-search alert. The company is the employer link that
    follows a vacancy link; unknown values stay None."""
    out: dict[str, Posting] = {}
    last: Posting | None = None
    for href, text in m.links:
        h = unquote(unquote(href))
        v = HH_VACANCY.search(h)
        if v and text and len(text) > 2:
            tld, vid = v.group(1), v.group(2)
            if vid not in out:
                last = Posting(source="hh_email", kind="aggregator", source_job_id=vid, company="", title=text,
                               url=f"https://hh.{tld}/vacancy/{vid}", posted_at=m.ts,
                               extra={"via": "hh alert e-mail"})
                out[vid] = last
            continue
        if last is not None and not last.company and "/employer/" in h and text:
            last.company = text
    return list(out.values())


def process(conn: sqlite3.Connection, clock: Clock, cfg: AppConfig, kb: KnowledgeBase | None, router,
            notifier: Notifier, queue: Queue, *, collector=None, limit: int = 100) -> InboxResult:
    res = InboxResult()
    rows = conn.execute("SELECT * FROM messages WHERE processed_at IS NULL ORDER BY ts LIMIT ?", (limit,)).fetchall()
    for row in rows:
        if control.is_paused(conn):
            break
        m = _message(row)
        try:
            cat = _process_one(conn, clock, cfg, kb, router, notifier, queue, row, m, collector, res)
        except Exception as e:
            log.exception("inbox: message failed")
            res.errors += 1
            cat = "error"
            conn.execute("UPDATE messages SET data_json = json_set(data_json, '$.error', ?) WHERE id = ?",
                         (f"{type(e).__name__}: {str(e)[:200]}", row["id"]))
        conn.execute("UPDATE messages SET processed_at = ?, category = COALESCE(category, ?) WHERE id = ?",
                     (iso(clock.now()), cat, row["id"]))

        res.processed += 1
        res.categories[cat] = res.categories.get(cat, 0) + 1
    return res


def _process_one(conn, clock, cfg, kb, router, notifier, queue, row, m: InMessage, collector, res) -> str:
    conv_id = row["conversation_id"]
    if m.direction == "out":
        if conv_id:
            _on_outbound(conn, clock, conv_id, m)
        return "outbound"
    t = _triage(cfg, router, m)
    conn.execute("UPDATE messages SET category = ?, data_json = json_set(data_json, '$.triage', json(?)) WHERE id = ?",
                 (t.category, json.dumps({"by": t.by, "questions": t.questions, "topics": sorted(t.topics),
                                          "summary": t.summary}, ensure_ascii=False), row["id"]))
    if t.category == "job_alert":
        postings = alert_postings(m)
        if postings and collector is not None:
            r = collector.ingest_postings("hh_email", postings)
            res.postings += r.kept
        if conv_id:
            _walk_to_waiting(conn, clock, conv_id)
        return t.category
    if conv_id is None:
        return t.category
    conv = _conv(conn, conv_id)
    app_id = conv["application_id"]
    title = _title(conn, app_id, m)
    if m.invites:
        recorded = record_invites(conn, clock, cfg, conv_id, app_id, m)
        if recorded:
            notifier.send(f"📅 {title}: приглашение в календаре — " + "; ".join(recorded)
                          + "\nПринять или перенести — в календаре/письме, сам. Напомню за сутки и за час.")
            if t.category not in tr.NEEDS_REPLY:
                t.category = "invite"
    target = {"rejection": "rejected", "invite": "interview", "screening_question": "screening",
              "info_request": "replied", "ack": "applied"}.get(t.category)
    if app_id and target:
        moved = advance_application(conn, clock, app_id, target, evidence=f"{m.channel}:{m.subject[:120]}")
        if t.category == "ack" and moved:
            notifier.send(f"✅ {title}: письмо подтверждает, что отклик дошёл — статус applied.", silent=True)
    state = _reopen(conn, clock, conv_id)
    if state == "new":
        _walk(conn, clock, conv_id, ["triaged"], data={"category": t.category, "by": t.by})
        state = "triaged"
    if t.category == "rejection":
        _walk_to_waiting(conn, clock, conv_id)
        notifier.send(f"❌ {title}: отказ.\n«{_snippet(m.text)}»", silent=True)
    elif t.category in tr.NEEDS_REPLY:
        if state == "approved":
            notifier.send(f"✉️ {title}: новое письмо, а черновик на прошлое уже в черновиках — посмотри тред.\n"
                          f"Тема: {m.subject}")
        elif kb is None:
            notifier.send(f"✉️ {title}: {t.category}, но база знаний не загрузилась — черновик не сделан.\n"
                          f"Тема: {m.subject}")
        else:
            make_draft(conn, clock, cfg, kb, router, notifier, queue, conv_id, m, t)
            res.drafts += 1
    else:
        _walk_to_waiting(conn, clock, conv_id)
        if t.category == "other" and not m.auto:
            notifier.send(f"✉️ {title}: письмо, которое я не разобрал — прочитай сам.\nТема: {m.subject}\n"
                          f"«{_snippet(m.text)}»", silent=True)
    return t.category


def _walk_to_waiting(conn, clock, conv_id) -> None:
    state = _conv(conn, conv_id)["state"]
    steps = {"new": ["triaged", "waiting"], "triaged": ["waiting"], "draft_ready": ["triaged", "waiting"],
             "sent": ["waiting"]}.get(state, [])
    _walk(conn, clock, conv_id, steps)


def _snippet(text: str, n: int = 240) -> str:
    t = " ".join(text.split())
    return t[:n] + ("…" if len(t) > n else "")


def _title(conn, app_id, m: InMessage) -> str:
    if app_id:
        j = conn.execute("SELECT j.company, j.title FROM applications a JOIN jobs j ON j.id = a.job_id "
                         "WHERE a.id = ?", (app_id,)).fetchone()
        if j:
            return f"{j['company']} — {j['title']}"
    return f"{m.from_name or m.from_addr}"


# --- drafts --------------------------------------------------------------------------------


def make_draft(conn, clock, cfg: AppConfig, kb: KnowledgeBase, router, notifier: Notifier, queue: Queue,
               conv_id: str, m: InMessage, t: tr.Triage) -> replymod.Draft:
    conv = _conv(conn, conv_id)
    job = None
    if conv["application_id"]:
        job = conn.execute("SELECT j.* FROM applications a JOIN jobs j ON j.id = a.job_id WHERE a.id = ?",
                           (conv["application_id"],)).fetchone()
    slots = None
    if t.category == "invite" or "interview_scheduling" in t.topics:
        slots = cal.slot_lines(cfg.secrets.get("CALENDAR_ICS_URL"), clock.now(), tz=cfg.settings.timezone,
                               country=job["country"] if job else None,
                               lang=replymod.lang_of(f"{m.subject}\n{m.text}"))
    d = replymod.build(kb, cfg.policy, router, message=m.text, subject=m.subject, to_name=m.from_name,
                       category=t.category, questions=t.questions, topics=t.topics,
                       company=job["company"] if job else None, job_title=job["title"] if job else None,
                       job_country=job["country"] if job else None, slots=slots)
    booking = cal.booking_links(m.text, m.links)
    data = json.loads(conv["data_json"] or "{}")
    n = int(data.get("drafts", 0)) + 1
    web_only = m.channel == "gmail" and bool(NO_REPLY.search(m.from_addr))
    data.update({"drafts": n, "draft": {
        "n": n, "text": d.text, "lang": d.lang, "ok": d.ok, "placeholders": d.placeholders,
        "topics": sorted(d.topics), "generated": d.generated, "notes": d.notes, "category": t.category,
        "reply_to": {"ext_id": m.ext_id, "references": m.references, "addr": m.from_addr, "subject": m.subject,
                     "thread_key": m.thread_key},
        "web_only": web_only, "booking": booking, "slots": slots or [],
        "unmatched": [f"{c.kind} «{c.text}»" for c in (d.truth.unmatched if d.truth else [])]}})
    _save_conv(conn, conv_id, data)
    transition(conn, clock, "conversation", conv_id, "draft_ready", actor="inbox",
               data={"n": n, "category": t.category, "topics": sorted(d.topics)})
    decision = decide_message(cfg.policy, "reply", d.topics)
    if decision.route is Route.SUBMIT:
        clean, _ = clean_approvals(conn)
        if clean < cfg.policy.l2_min_clean_approvals:   # L2 is earned: drafts sent unedited first
            decision = Decision(Route.ASK, [f"L2 for replies needs {cfg.policy.l2_min_clean_approvals} drafts sent "
                                            f"unedited, so far {clean}"])
    if decision.route is Route.SUBMIT and d.ok and not d.placeholders and not web_only:
        msg = approve(conn, clock, queue, conv_id, actor="policy")
        notifier.send(f"📥 {_title(conn, conv['application_id'], m)}: черновик ответа кладу в черновики "
                      f"(автономия L2). {msg}")
        return d
    send_card(conn, notifier, conv_id, m, t, decision.reasons)
    return d


def card_text(conn, conv_id: str, m: InMessage, t: tr.Triage, reasons: list[str]) -> str:
    conv = _conv(conn, conv_id)
    data = json.loads(conv["data_json"] or "{}")
    d = data["draft"]
    lines = [f"✉️ {_title(conn, conv['application_id'], m)}", f"От: {m.from_name} <{m.from_addr}>".strip(),
             f"Тема: {m.subject}",
             f"Категория: {t.category}" + (f" · темы: {', '.join(d['topics'])}" if d["topics"] else "")]
    if data.get("link_ambiguous"):
        lines.append("⚠️ Привязка к заявке неоднозначна: проверь, о какой вакансии речь.")
    if t.questions:
        lines += ["", "Вопросы:"] + [f"• {q[:200]}" for q in t.questions[:8]]
    else:
        lines += ["", f"«{_snippet(m.text, 400)}»"]
    lines += ["", "— Черновик ответа —", d["text"], "—"]
    if d["placeholders"]:
        lines.append("✍️ Заполни перед отправкой: " + ", ".join(d["placeholders"]))
    if not d["ok"]:
        lines.append("⛔ Проверка правды: утверждения без факта в базе — " + "; ".join(d["unmatched"][:6])
                     + ". Такой черновик не отправить: «Переделать» или ответь сам.")
    if d.get("booking"):
        lines.append("📅 Ссылка для записи (бронируешь сам): " + " ".join(d["booking"][:3]))
    if d.get("slots"):
        lines.append("🗓 Окна взяты из твоего календаря (iCal); проверь перед отправкой.")
    if d["web_only"]:
        lines.append("ℹ️ Адрес no-reply: ответ — на сайте/в ЛК (ссылки в письме), текст можно скопировать.")
    for n in d["notes"]:
        lines.append(f"ℹ️ {n}")
    if reasons:
        lines.append("Почему нужен ты: " + "; ".join(reasons))
    return "\n".join(lines)


def card_buttons(conn, conv_id: str) -> list[list[tuple[str, str]]]:
    conv = _conv(conn, conv_id)
    d = json.loads(conv["data_json"] or "{}")["draft"]
    rid = conv["rowid"]
    rows = []
    if d["ok"] and not d["web_only"]:
        label = {"telegram": "📝 Черновиком в Telegram", "whatsapp": "💬 Ссылка для WhatsApp"}.get(
            conv["channel"], "📥 В черновики Gmail")
        rows.append([(label, f"m:ok:{rid}")])
    if d.get("placeholders"):
        rows.append([("✍️ Заполнить " + d["placeholders"][0][9:-2][:30], f"m:fill:{rid}")])
    rows.append([("🔁 Переделать", f"m:redo:{rid}"),
                 ("✅ Ответил сам" if d["web_only"] else "🙅 Не отвечать", f"m:no:{rid}")])
    return rows


def send_card(conn, notifier: Notifier, conv_id: str, m: InMessage, t: tr.Triage, reasons: list[str]) -> bool:
    return notifier.send(card_text(conn, conv_id, m, t, reasons), buttons=card_buttons(conn, conv_id))


def approve(conn, clock, queue: Queue, conv_id: str, actor: str = "owner") -> str:
    conv = _conv(conn, conv_id)
    data = json.loads(conv["data_json"] or "{}")
    d = data["draft"]
    key = f"draft:{conv_id}:{d['n']}"
    transition(conn, clock, "conversation", conv_id, "approved", expected="draft_ready", actor=actor,
               data={"n": d["n"]})
    if conv["channel"] == "whatsapp":
        # No WhatsApp API for personal chats: the owner opens the link, WhatsApp shows the text, they send.
        _walk(conn, clock, conv_id, ["sent", "waiting"], data={"via": "wa.me link"}, actor=actor)
        return "Открой ссылку — WhatsApp подставит текст, отправишь сам:\n" + wa_link(
            d["reply_to"].get("thread_key"), d["text"])
    task_type = TG_DRAFT if conv["channel"] == "telegram" else DRAFT
    queue.enqueue(task_type, {"conv_id": conv_id, "key": key}, idempotency_key=key, max_attempts=3)
    if conv["channel"] == "telegram":
        return "Черновик появится в чате Telegram на ноуте; отправишь сам."
    return "Черновик появится в Gmail в течение минуты; отправишь сам (код письма не отправляет)."


def message_decision(conn: sqlite3.Connection, clock: Clock, queue: Queue, action: str, rowid: int) -> str:
    conv = conn.execute("SELECT rowid, * FROM conversations WHERE rowid = ?", (rowid,)).fetchone()
    if conv is None:
        return "Переписка не найдена."
    if conv["state"] != "draft_ready":
        return f"Переписка уже в состоянии {conv['state']}: черновик устарел."
    data = json.loads(conv["data_json"] or "{}")
    d = data.get("draft") or {}
    if action == "ok":
        if not d.get("ok") or d.get("web_only"):
            return "Этот черновик нельзя одобрить (проверка правды или no-reply адрес)."
        return "📥 Одобрено. " + approve(conn, clock, queue, conv["id"])
    if action == "redo":
        _walk(conn, clock, conv["id"], ["triaged"], data={"redo": d.get("n")}, actor="owner")
        queue.enqueue(REPLY, {"conv_id": conv["id"]}, idempotency_key=f"reply:{conv['id']}:{int(d.get('n', 0)) + 1}")
        return "🔁 Переделаю черновик в течение минуты."
    if action == "fill":
        if not d.get("placeholders"):
            return "Заполнять нечего."
        control.set_flag(conn, clock, FILL_FLAG, f"{rowid}|{iso(clock.now())}", actor="owner")
        return f"Напиши следующим сообщением: {d['placeholders'][0][9:-2]}. (/cancel — передумал)"
    if action == "no":
        _walk(conn, clock, conv["id"], ["triaged", "waiting"], data={"declined": d.get("n")}, actor="owner")
        record_event(conn, clock, entity="conversation", entity_id=conv["id"], kind="reply_outcome", actor="owner",
                     data={"clean": False, "via": "declined"})
        return "Ок, без ответа." if not d.get("web_only") else "Ок, отметил: ответил сам."
    return "Неизвестное действие."


def redraft(conn, clock, cfg, kb, router, notifier, queue, conv_id: str) -> bool:
    """`inbox.reply` task: a new draft for the latest inbound message of a triaged conversation."""
    conv = _conv(conn, conv_id)
    if conv is None or conv["state"] != "triaged":
        return False
    row = conn.execute("SELECT * FROM messages WHERE conversation_id = ? AND direction = 'in' ORDER BY ts DESC "
                       "LIMIT 1", (conv_id,)).fetchone()
    if row is None:
        return False
    m = _message(row)
    tri = json.loads(row["data_json"] or "{}").get("triage") or {}
    t = tr.Triage(row["category"] or "info_request", tri.get("by", "?"), tri.get("questions", []),
                  set(tri.get("topics", [])), tri.get("summary", ""))
    make_draft(conn, clock, cfg, kb, router, notifier, queue, conv_id, m, t)
    return True


def draft_bytes(conn, cfg: AppConfig, kb: KnowledgeBase | None, conv_id: str, from_addr: str) -> bytes:
    data = json.loads(_conv(conn, conv_id)["data_json"] or "{}")
    d = data["draft"]
    rt = d["reply_to"]
    return build_reply(from_name=kb.profile.name if kb else "", from_addr=from_addr, to_addr=rt["addr"],
                       subject=rt["subject"], body=d["text"], in_reply_to=rt["ext_id"], references=rt["references"])


# --- outbound and ghosting -----------------------------------------------------------------


def _clean(draft: str, sent: str) -> bool:
    a, b = " ".join(draft.split()).lower(), " ".join(sent.split()).lower()
    if not a or replymod.PLACEHOLDER.search(draft):
        return False
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.97


def _on_outbound(conn, clock, conv_id: str, m: InMessage) -> None:
    conv = _conv(conn, conv_id)
    data = json.loads(conv["data_json"] or "{}")
    state = conv["state"]
    if state == "approved":
        clean = _clean((data.get("draft") or {}).get("text", ""), m.text)
        _walk(conn, clock, conv_id, ["sent", "waiting"], data={"clean": clean}, actor="owner")
        record_event(conn, clock, entity="conversation", entity_id=conv_id, kind="reply_outcome", actor="owner",
                     data={"clean": clean, "via": "draft"})
    elif state in ("draft_ready", "new", "triaged"):
        steps = {"draft_ready": ["triaged", "waiting"], "new": ["triaged", "waiting"], "triaged": ["waiting"]}[state]
        _walk(conn, clock, conv_id, steps, data={"owner": "replied without the draft"}, actor="owner")
        if state == "draft_ready":
            record_event(conn, clock, entity="conversation", entity_id=conv_id, kind="reply_outcome", actor="owner",
                         data={"clean": False, "via": "self"})


def clean_approvals(conn: sqlite3.Connection) -> tuple[int, int]:
    """(drafts sent unedited, all reply outcomes): policy.yaml l2_min_clean_approvals is checked on it."""
    rows = conn.execute("SELECT data_json FROM events WHERE entity = 'conversation' AND kind = 'reply_outcome'"
                        ).fetchall()
    vals = [json.loads(r[0]).get("clean") for r in rows]
    return sum(1 for v in vals if v), len(vals)


def mark_ghosted(conn: sqlite3.Connection, clock: Clock, days: int) -> int:
    cutoff = iso(clock.now() - timedelta(days=days))
    rows = conn.execute("SELECT id FROM applications WHERE state = 'applied' AND updated_at < ?", (cutoff,)).fetchall()
    n = 0
    for r in rows:
        heard = conn.execute("SELECT 1 FROM messages s JOIN conversations c ON c.id = s.conversation_id "
                             "WHERE c.application_id = ? AND s.direction = 'in' AND s.ts >= ? LIMIT 1",
                             (r["id"], cutoff)).fetchone()
        if not heard:
            transition(conn, clock, "application", r["id"], "ghosted", expected="applied", actor="inbox",
                       data={"silent_days": days})
            n += 1
    return n


# --- the run -------------------------------------------------------------------------------


def run(conn: sqlite3.Connection, clock: Clock, cfg: AppConfig, kb: KnowledgeBase | None, router,
        notifier: Notifier, queue: Queue, mailbox_factory, *, collector=None, parse=None) -> InboxResult:
    from ..adapters.inbox.mail import parse_email

    parse = parse or parse_email
    res = InboxResult()
    if control.is_paused(conn):
        return res
    ic = cfg.settings.inbox
    owner = owner_addresses(cfg, kb)
    since = (clock.now() - timedelta(days=ic.first_run_days)).date()
    if mailbox_factory is not None:
        with mailbox_factory() as mb:
            for folder in ("inbox", "sent"):
                flag = f"inbox.cursor.{folder}"
                cursor = json.loads(control.get_flag(conn, flag) or "null")
                raws, new_cursor = mb.fetch_new(folder, cursor, since=since, limit=ic.max_per_run)
                res.fetched += len(raws)
                for raw, thrid in raws:
                    try:
                        m = parse(raw, thread_key=thrid, body_chars=ic.body_chars)
                    except Exception:
                        log.exception("inbox: unparsable message")
                        res.errors += 1
                        continue
                    if folder == "sent":
                        m.direction = "out"
                    if store(conn, clock, cfg, m, owner=owner):
                        res.stored += 1
                    else:
                        res.skipped += 1
                control.set_flag(conn, clock, flag, json.dumps(new_cursor), actor="inbox")
    p = process(conn, clock, cfg, kb, router, notifier, queue, collector=collector)
    res.processed, res.categories, res.drafts, res.postings = p.processed, p.categories, p.drafts, p.postings
    res.errors += p.errors
    res.ghosted = mark_ghosted(conn, clock, ic.ghosted_after_days)
    res.reminders = remind(conn, clock, cfg, notifier)
    return res


# --- interviews (S7) -------------------------------------------------------------------------


def record_invites(conn, clock, cfg: AppConfig, conv_id: str, app_id: str | None, m: InMessage) -> list[str]:
    """Interviews from calendar invitations, stored on the conversation (and the application)."""
    from zoneinfo import ZoneInfo

    conv = _conv(conn, conv_id)
    data = json.loads(conv["data_json"] or "{}")
    known = {(i["uid"], i["start"]) for i in data.get("interviews", [])}
    out = []
    for ics in m.invites:
        for e in cal.parse_ics(ics, cfg.settings.timezone):
            if e.end < clock.now() or (e.uid, iso(e.start)) in known:
                continue
            data.setdefault("interviews", []).append({"uid": e.uid, "start": iso(e.start), "end": iso(e.end),
                                                      "summary": e.summary, "url": e.url, "reminded": []})
            known.add((e.uid, iso(e.start)))
            local = e.start.astimezone(ZoneInfo(cfg.settings.timezone))
            out.append(f"{local:%a %d.%m %H:%M} (Алматы) {e.summary}".strip() + (f" {e.url}" if e.url else ""))
    if out:
        _save_conv(conn, conv_id, data)
        if app_id:
            advance_application(conn, clock, app_id, "interview", evidence=f"calendar invite: {m.subject[:100]}")
    return out


REMINDERS = ((24 * 3600, "24h", "завтра"), (3600 + 15 * 60, "1h", "через час"))


def remind(conn: sqlite3.Connection, clock: Clock, cfg: AppConfig, notifier: Notifier) -> int:
    """Telegram reminders a day and an hour before each recorded interview, with what was sent."""
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo

    sent = 0
    now = clock.now()
    for conv in conn.execute("SELECT id, application_id, data_json FROM conversations "
                             "WHERE json_extract(data_json, '$.interviews') IS NOT NULL").fetchall():
        data = json.loads(conv["data_json"])
        changed = False
        for iv in data["interviews"]:
            start = _dt.fromisoformat(iv["start"].replace("Z", "+00:00"))
            left = (start - now).total_seconds()
            for window, tag, word in REMINDERS:
                if 0 < left <= window and tag not in iv["reminded"]:
                    # the 24h reminder is pointless once the 1h one is due
                    if tag == "24h" and left <= REMINDERS[1][0]:
                        iv["reminded"].append(tag)
                        changed = True
                        continue
                    local = start.astimezone(ZoneInfo(cfg.settings.timezone))
                    lines = [f"⏰ Интервью {word}: {local:%a %d.%m %H:%M} (Алматы) — {iv['summary']}"]
                    if iv.get("url"):
                        lines.append(iv["url"])
                    if conv["application_id"]:
                        j = conn.execute("SELECT j.company, j.title, j.url, a.data_json FROM applications a "
                                         "JOIN jobs j ON j.id = a.job_id WHERE a.id = ?",
                                         (conv["application_id"],)).fetchone()
                        if j:
                            lines.append(f"{j['company']} — {j['title']}\n{j['url']}")
                            arts = json.loads(j["data_json"] or "{}").get("artifacts", {})
                            if arts.get("pdf") or arts.get("tex"):
                                lines.append(f"Отправленное CV: {arts.get('pdf') or arts.get('tex')}")
                    # at most once, also when the inbox run and the hourly selfcheck overlap (S7 review)
                    key = f"remind:{conv['id']}:{iv['start']}:{tag}"
                    se = control.SideEffects(conn, clock)
                    if control.is_paused(conn):
                        continue                 # not sent and not marked: it goes out after /resume (S7 review)
                    if not se.plan(key, "reminder"):
                        iv["reminded"].append(tag)   # another run already sent it
                        changed = True
                        continue
                    ok = notifier.send("\n".join(lines))
                    se.mark(key, "done" if ok else "failed")
                    if not ok:
                        se.resolve(key, "reset", actor="inbox")    # not delivered: try again next run
                        continue
                    iv["reminded"].append(tag)
                    changed = True
                    sent += 1
        if changed:
            _save_conv(conn, conv["id"], data)
    return sent


# --- WhatsApp through the bot (S7) -----------------------------------------------------------


def wa_link(thread_key: str | None, text: str) -> str:
    from urllib.parse import quote

    digits = re.sub(r"\D", "", thread_key or "")
    return f"https://wa.me/{digits if len(digits) >= 7 else ''}?text={quote(text)}"


def whatsapp_inbound(conn, clock, cfg: AppConfig, queue: Queue, raw: str) -> str:
    """`/wa <phone or name>` + newline + the pasted message. Stored as a WhatsApp conversation and
    triaged like mail; the reply comes back as a wa.me link (WhatsApp has no API for personal chats)."""
    head, _, text = raw.strip().partition("\n")
    head, text = head.strip(), text.strip()
    if not head or not text:
        return ("Формат: /wa +77001234567 Имя\n<текст сообщения из WhatsApp>\n"
                "(первая строка — номер или имя рекрутера, дальше вставь сообщение)")
    phone = re.search(r"\+?\d[\d\s()-]{6,}\d", head)
    digits = re.sub(r"\D", "", phone.group(0)) if phone else ""
    name = (head.replace(phone.group(0), "") if phone else head).strip(" ,-")
    key = digits or name.lower()
    now = iso(clock.now())
    m = InMessage(channel="whatsapp", ext_id=f"wa:{key}:{hashlib.sha1(text.encode()).hexdigest()[:12]}", ts=now,
                  from_addr=f"wa:{digits}" if digits else f"wa:{name}", from_name=name, text=text[:6000],
                  thread_key=digits or key)
    if not store(conn, clock, cfg, m, owner=set(), force=True):
        return "Это сообщение уже было."
    queue.enqueue(PROCESS, {}, idempotency_key=f"inbox.process:{m.id}")
    return "Принял. Разберу и пришлю черновик в течение минуты."


# --- filling placeholders from Telegram (S7) -------------------------------------------------

FILL_FLAG = "inbox.fill_pending"


FILL_TTL_S = 600


def _pending(conn, clock) -> str | None:
    """rowid of the draft waiting for a value; the request expires after 10 minutes (S7 review)."""
    from datetime import datetime as _dt

    raw = control.get_flag(conn, FILL_FLAG) or ""
    rowid, _, at = raw.partition("|")
    if not rowid or not at:
        return None
    age = (clock.now() - _dt.fromisoformat(at.replace("Z", "+00:00"))).total_seconds()
    return rowid if age <= FILL_TTL_S else None


def fill_pending(conn, clock=None) -> bool:
    from ..clock import SystemClock

    return _pending(conn, clock or SystemClock()) is not None


def fill_next(conn, clock, text: str) -> tuple[str, list | None]:
    """The owner's next plain message replaces the first placeholder of the pending draft. It is the
    owner's own text, so it is not truth-gated; the updated card comes back with its buttons."""
    rowid = _pending(conn, clock)
    control.set_flag(conn, clock, FILL_FLAG, "", actor="owner")
    if rowid is None:
        return "Запрос на заполнение истёк: нажми «✍️ Заполнить» ещё раз.", None
    if text.strip() in ("/cancel", "отмена"):
        return "Ок, не заполняю.", None
    conv = conn.execute("SELECT rowid, * FROM conversations WHERE rowid = ?", (int(rowid),)).fetchone()
    if conv is None or conv["state"] != "draft_ready":
        return "Черновик уже неактуален.", None
    data = json.loads(conv["data_json"] or "{}")
    d = data["draft"]
    hole = d["placeholders"].pop(0)
    d["text"] = d["text"].replace(hole, text.strip(), 1)
    d.setdefault("filled", []).append(hole)
    _save_conv(conn, conv["id"], data)
    record_event(conn, clock, entity="conversation", entity_id=conv["id"], kind="note", actor="owner",
                 data={"filled": hole})
    left = (" Осталось: " + ", ".join(d["placeholders"])) if d["placeholders"] else " Всё заполнено."
    return f"✍️ Вставил.{left}\n\n— Черновик —\n{d['text']}", card_buttons(conn, conv["id"])
