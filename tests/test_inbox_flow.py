"""S5 end to end with a fake mailbox: real DB, triage, funnel moves, drafts, approve -> Gmail draft once,
the owner's sent reply closing the loop, job alerts, ghosting, and the Telegram userbot path."""
from email import message_from_bytes, policy

import pytest

from jobpipe.adapters.notifier.telegram import RecordingNotifier
from jobpipe.adapters.storage.db import open_db
from jobpipe.api.worker import WorkerAPI
from jobpipe.services import control
from jobpipe.services import inbox as ib
from jobpipe.services import kb as kbmod
from jobpipe.services.collect import Collector
from jobpipe.services.events import create_entity, transition
from jobpipe.services.queue import Queue
from jobpipe.workers.handlers import HANDLERS
from jobpipe.workers.runner import Runner

from .conftest import REPO

MAIL = REPO / "tests" / "fixtures" / "mail"
KB = kbmod.load(REPO / "config.example" / "kb")


def raw(name: str) -> bytes:
    return (MAIL / name).read_bytes()


class FakeMailbox:
    def __init__(self):
        self.boxes = {"inbox": [], "sent": []}
        self.drafts: list[bytes] = []
        self.fail_append = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def add(self, folder, name_or_bytes, thread):
        data = raw(name_or_bytes) if isinstance(name_or_bytes, str) else name_or_bytes
        self.boxes[folder].append((data, thread))

    def fetch_new(self, folder, cursor, *, since, limit):
        box = self.boxes[folder]
        last = (cursor or {}).get("last_uid", 0)
        return box[last:last + limit], {"uidvalidity": 1, "last_uid": min(len(box), last + limit)}

    def append_draft(self, data: bytes) -> None:
        if self.fail_append:
            raise OSError("connection reset")
        self.drafts.append(data)


APPLIED_PATH = ["tailoring", "ready", "filling", "submitting", "applied"]


def add_app(conn, clock, company, title, *, country="DE", state="applied"):
    jid = f"gh:{company.lower().split()[0]}:1"
    create_entity(conn, clock, "job", jid, columns={"source": "greenhouse", "kind": "ats", "company": company,
                                                    "title": title, "url": f"https://boards.greenhouse.io/{jid}",
                                                    "country": country})
    app = f"app:{jid}"
    key = f"apply:{company.lower().split()[0]}:{jid}"
    create_entity(conn, clock, "application", app, columns={"job_id": jid, "idempotency_key": key})
    path = APPLIED_PATH if state == "applied" else [*APPLIED_PATH[:-1], state]
    for s in path:
        transition(conn, clock, "application", app, s)
    if state == "submit_unknown":
        se = control.SideEffects(conn, clock)
        se.plan(key, "apply")
        se.mark(key, "unknown")
    return app, key


@pytest.fixture
def env(app_cfg, clock):
    conn = open_db(app_cfg.paths.db_path, clock)
    apps = {
        "acme": add_app(conn, clock, "Acme", "C++ Engineer", state="submit_unknown"),
        "beta": add_app(conn, clock, "Beta Corp", "Backend Engineer"),
        "gamma": add_app(conn, clock, "Gamma", "Backend Developer", country="KZ"),
    }
    mb = FakeMailbox()
    notifier = RecordingNotifier()
    q = Queue(conn, clock, app_cfg.settings.queue)
    yield conn, apps, mb, notifier, q
    conn.close()


def run(conn, clock, cfg, mb, notifier, q, router=None):
    return ib.run(conn, clock, cfg, KB, router, notifier, q, lambda: mb,
                  collector=Collector(conn, clock, cfg, None, notifier))


def state(conn, table, id_):
    return conn.execute(f"SELECT state FROM {table} WHERE id = ?", (id_,)).fetchone()[0]


def runner(conn, clock, cfg, mb, notifier):
    return Runner(conn, clock, cfg, HANDLERS, notifier,
                  services={"kb": lambda: KB, "mailbox": lambda: mb, "router": lambda: None})


def test_full_inbox_round(env, app_cfg, clock):
    conn, apps, mb, notifier, q = env
    for name, thread in (("ack_greenhouse.eml", "T1"), ("screening.eml", "T2"), ("rejection.eml", "T3"),
                         ("invite_ru.eml", "T4"), ("hh_alert.eml", "T5"), ("personal.eml", "T6")):
        mb.add("inbox", name, thread)
    res = run(conn, clock, app_cfg, mb, notifier, q)

    # privacy: the personal mail was not stored; the alert became postings, not a conversation
    assert res.fetched == 6 and res.stored == 5 and res.skipped == 1
    assert conn.execute("SELECT COUNT(*) FROM messages WHERE subject LIKE 'Dinner%'").fetchone()[0] == 0
    assert res.categories == {"ack": 1, "screening_question": 1, "rejection": 1, "invite": 1, "job_alert": 1}

    # funnel: ack confirmed the unknown submit, the screening moved it on; rejection; invite -> interview
    acme_app, acme_key = apps["acme"]
    assert state(conn, "applications", acme_app) == "screening"
    assert control.SideEffects(conn, clock).status(acme_key) == "done"
    assert state(conn, "applications", apps["beta"][0]) == "rejected"
    assert state(conn, "applications", apps["gamma"][0]) == "interview"

    # job alert -> postings through the collector, with the employer link as the company
    jobs = {r["id"]: r["company"] for r in conn.execute("SELECT id, company FROM jobs WHERE source = 'hh_email'")}
    assert jobs == {"hh_email:100000001": "Delta Systems", "hh_email:100000002": "Epsilon Trading"}

    # drafts: one card per conversation that needs a reply, with the right buttons
    assert res.drafts == 2
    cards = [(m, b) for m, b in zip(notifier.messages, notifier.buttons, strict=True) if b]
    assert len(cards) == 2
    screening_card = next(m for m, _ in cards if "Acme" in m)
    assert "[[ВПИШИ: зарплатные ожидания]]" in screening_card
    assert "Two weeks after signing the offer." in screening_card
    assert "I would need visa sponsorship" in screening_card            # DE job: sponsorship needed
    gamma_card = next(m for m, _ in cards if "Gamma" in m)
    assert "Здравствуйте, Мария!" in gamma_card and "[[ВПИШИ: удобное время для звонка]]" in gamma_card
    assert any("отказ" in m for m in notifier.messages)

    conv = conn.execute("SELECT rowid, id, state, application_id FROM conversations WHERE id = "
                        "(SELECT conversation_id FROM messages WHERE thread_key = 'T2')").fetchone()
    assert conv["application_id"] == acme_app
    assert conv["state"] == "draft_ready"
    buttons = next(b for m, b in cards if "Acme" in m)
    assert buttons[0][0] == ("📥 В черновики Gmail", f"m:ok:{conv['rowid']}")

    # approve -> one inbox.draft task -> the runner appends exactly one Gmail draft, threaded
    out = ib.message_decision(conn, clock, q, "ok", conv["rowid"])
    assert "Gmail" in out and state(conn, "conversations", conv["id"]) == "approved"
    assert "устарел" in ib.message_decision(conn, clock, q, "ok", conv["rowid"])   # double tap
    r = runner(conn, clock, app_cfg, mb, notifier)
    assert r.run(max_seconds=5).done == 1
    assert len(mb.drafts) == 1
    d = message_from_bytes(mb.drafts[0], policy=policy.default)
    assert d["In-Reply-To"] == "<scr-1@acme.example>" and d["To"] == "jane@acme.example"
    assert d["From"] == "Alex Example <alex@example.com>"
    assert r.run(max_seconds=5).done == 0 and len(mb.drafts) == 1
    q.enqueue("inbox.draft", {"conv_id": conv["id"], "key": f"draft:{conv['id']}:1"})   # a stray duplicate task
    r.run(max_seconds=5)
    assert len(mb.drafts) == 1                                                       # the key holds: never twice

    # the owner sends it (after filling the placeholder) -> sent -> waiting, recorded as edited
    body = d.get_content().replace("[[ВПИШИ: зарплатные ожидания]]", "My expectation is 5000 EUR gross.")
    mb.add("sent", raw("owner_reply.eml").replace(b"BODY_PLACEHOLDER", body.encode()), "T2")
    run(conn, clock, app_cfg, mb, notifier, q)
    assert state(conn, "conversations", conv["id"]) == "waiting"
    assert ib.clean_approvals(conn) == (0, 1)

    # a new mail in the same thread reopens the conversation
    reply2 = raw("screening.eml").replace(b"<scr-1@acme.example>", b"<scr-2@acme.example>").replace(
        b"What is your notice period?", b"Could you also share your GitHub profile?")
    mb.add("inbox", reply2, "T2")
    run(conn, clock, app_cfg, mb, notifier, q)
    assert state(conn, "conversations", conv["id"]) == "draft_ready"
    assert state(conn, "applications", acme_app) == "screening"             # never moved backwards


def test_redo_and_decline(env, app_cfg, clock):
    conn, _apps, mb, notifier, q = env
    mb.add("inbox", "screening.eml", "T2")
    run(conn, clock, app_cfg, mb, notifier, q)
    conv = conn.execute("SELECT rowid, id FROM conversations").fetchone()
    assert "Переделаю" in ib.message_decision(conn, clock, q, "redo", conv["rowid"])
    assert state(conn, "conversations", conv["id"]) == "triaged"
    runner(conn, clock, app_cfg, mb, notifier).run(max_seconds=5)
    assert state(conn, "conversations", conv["id"]) == "draft_ready"
    data = conn.execute("SELECT json_extract(data_json, '$.drafts') FROM conversations").fetchone()[0]
    assert data == 2
    assert "без ответа" in ib.message_decision(conn, clock, q, "no", conv["rowid"])
    assert state(conn, "conversations", conv["id"]) == "waiting"


def test_failed_append_is_unknown_and_never_retried(env, app_cfg, clock):
    conn, _apps, mb, notifier, q = env
    mb.add("inbox", "screening.eml", "T2")
    run(conn, clock, app_cfg, mb, notifier, q)
    conv = conn.execute("SELECT rowid, id FROM conversations").fetchone()
    ib.message_decision(conn, clock, q, "ok", conv["rowid"])
    mb.fail_append = True
    r = runner(conn, clock, app_cfg, mb, notifier)
    r.run(max_seconds=5)
    key = f"draft:{conv['id']}:1"
    assert control.SideEffects(conn, clock).status(key) == "unknown"
    assert any("мог не создаться" in m for m in notifier.messages)
    mb.fail_append = False
    r.run(max_seconds=5)
    assert mb.drafts == []                                                 # no blind retry


def test_truth_gate_failure_removes_the_approve_button(env, app_cfg, clock):
    conn, _apps, mb, notifier, q = env

    class Liar:
        def complete(self, task, variables, schema=None, personal=False):
            from jobpipe.services.reply import ReplyOut
            from jobpipe.services.triage import TriageOut

            if task == "triage":
                return type("C", (), {"data": TriageOut(category="screening_question",
                                                        questions=["What is your notice period?"])})()
            return type("C", (), {"data": ReplyOut(reply="Hello Jane, I led 40 engineers at Google.")})()

    mb.add("inbox", "screening.eml", "T2")
    run(conn, clock, app_cfg, mb, notifier, q, router=Liar())
    card = next(m for m, b in zip(notifier.messages, notifier.buttons, strict=True) if b)
    buttons = next(b for b in notifier.buttons if b)
    assert "⛔ Проверка правды" in card
    assert all(not d.startswith("m:ok") for row in buttons for _, d in row)
    conv = conn.execute("SELECT rowid FROM conversations").fetchone()
    assert "нельзя одобрить" in ib.message_decision(conn, clock, q, "ok", conv["rowid"])


def test_noreply_sender_gets_a_copyable_draft_without_gmail_button(env, app_cfg, clock):
    conn, _apps, mb, notifier, q = env
    m = raw("screening.eml").replace(b"jane@acme.example", b"no-reply@acme.example")
    mb.add("inbox", m, "T9")
    run(conn, clock, app_cfg, mb, notifier, q)
    buttons = next(b for b in notifier.buttons if b)
    assert [d for row in buttons for _, d in row if d.startswith("m:ok")] == []
    assert any("no-reply" in x for x in notifier.messages)


def test_ghosted_after_silence_and_paused_run_does_nothing(env, app_cfg, clock):
    conn, apps, mb, notifier, q = env
    clock.advance(22 * 86400)
    res = run(conn, clock, app_cfg, mb, notifier, q)
    assert res.ghosted == 2                                     # beta and gamma: applied, silent 22 days
    assert state(conn, "applications", apps["beta"][0]) == "ghosted"
    assert state(conn, "applications", apps["acme"][0]) == "submit_unknown"   # not applied: not ghosted
    mb.add("inbox", "rejection.eml", "T3")                     # a late rejection after silence
    run(conn, clock, app_cfg, mb, notifier, q)
    assert state(conn, "applications", apps["beta"][0]) == "rejected"
    control.set_flag(conn, clock, control.PAUSED, "1")
    mb.add("inbox", "screening.eml", "T2")
    assert run(conn, clock, app_cfg, mb, notifier, q).fetched == 0


def test_ambiguous_company_match_is_flagged(app_cfg, clock):
    conn = open_db(app_cfg.paths.db_path, clock)
    add_app(conn, clock, "Acme", "C++ Engineer")
    create_entity(conn, clock, "job", "gh:acme:2", columns={"source": "greenhouse", "kind": "ats", "company": "Acme",
                                                            "title": "Data Engineer", "url": "https://x.example/2"})
    create_entity(conn, clock, "application", "app:gh:acme:2", columns={"job_id": "gh:acme:2",
                                                                        "idempotency_key": "apply:acme:gh:acme:2"})
    for s in APPLIED_PATH:
        transition(conn, clock, "application", "app:gh:acme:2", s)
    from jobpipe.adapters.inbox.mail import parse_email

    m = parse_email(raw("screening.eml"))
    app, ambiguous = ib.link_application(conn, m)
    assert app == "app:gh:acme:1" and not ambiguous            # "C++ Engineer" in the subject decides
    m.subject, m.text = "Your application at Acme", "Hello, a few questions?"
    app, ambiguous = ib.link_application(conn, m)
    assert ambiguous


def test_telegram_userbot_path(env, app_cfg, clock):
    conn, _apps, mb, notifier, q = env
    from jobpipe.config.settings import Secrets

    app_cfg.secrets = Secrets({"WORKER_TOKEN": "t"})
    api = WorkerAPI(app_cfg, clock, notifier=notifier, kb_loader=lambda: KB)
    out = api.inbound([
        {"chat_id": 4242, "msg_id": 1, "out": False, "from_id": 4242, "from_name": "Olga", "username": "olga_hr",
         "text": "Hi! We have a C++ role at Zeta Labs. What is your notice period?", "ts": "2026-11-26T09:00:00Z"},
        {"chat_id": 4242, "msg_id": 1, "out": False, "text": "duplicate"},                  # same id: ignored
    ])
    assert out == {"stored": 1}
    r = runner(conn, clock, app_cfg, mb, notifier)
    r.run(max_seconds=5)                                        # inbox.process task -> triage + draft
    conv = conn.execute("SELECT rowid, id, state, channel FROM conversations").fetchone()
    assert conv["channel"] == "telegram" and conv["state"] == "draft_ready"
    buttons = next(b for b in notifier.buttons if b)
    assert buttons[0][0][0] == "📝 Черновиком в Telegram"
    ib.message_decision(conn, clock, q, "ok", conv["rowid"])
    r.run(max_seconds=5)                                        # tg.draft is not the VM's: stays pending
    assert api.lease_once("laptop") is None                     # the browser worker does not get it either
    task = api.lease_once("userbot", ["tg.draft"])
    assert task["chat_id"] == "4242" and "Two weeks after signing the offer." in task["text"]
    key = task["key"]
    assert control.SideEffects(conn, clock).status(key) == "planned"
    assert api.lease_once("userbot", ["tg.draft"]) is None
    api.result({"task_id": task["task_id"], "kind": "tg_draft", "app_id": task["app_id"],
                "result": {"key": key, "ok": True}}, None)
    assert control.SideEffects(conn, clock).status(key) == "done"
    # the owner's own message in the chat closes the loop
    api.inbound([{"chat_id": 4242, "msg_id": 2, "out": True, "text": task["text"], "ts": "2026-11-26T10:00:00Z"}])
    r.run(max_seconds=5)
    assert state(conn, "conversations", conv["id"]) == "waiting"
    assert ib.clean_approvals(conn) == (1, 1)                  # sent exactly as drafted


def test_clean_needs_no_placeholders_and_near_identical_text():
    assert ib._clean("Hello Jane,\nThanks!", "Hello  Jane, thanks!")
    assert not ib._clean("Hi [[ВПИШИ: x]]", "Hi 5")
    assert not ib._clean("Hello Jane, I can talk on Monday.", "Hello Jane, Tuesday is better for me.")


def test_retention_drops_old_mail_text_but_keeps_the_funnel(env, app_cfg, clock):
    from jobpipe.services.retention import run_retention

    conn, apps, mb, notifier, q = env
    mb.add("inbox", "rejection.eml", "T3")
    run(conn, clock, app_cfg, mb, notifier, q)
    clock.advance(121 * 86400)
    counts = run_retention(conn, clock, app_cfg.settings.retention)
    assert counts["messages_compacted"] == 1
    row = conn.execute("SELECT body, category, subject FROM messages").fetchone()
    assert row["body"] == "" and row["category"] == "rejection" and row["subject"]
    assert state(conn, "applications", apps["beta"][0]) == "rejected"
