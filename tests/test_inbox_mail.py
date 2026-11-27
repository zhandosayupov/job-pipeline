"""S5 mail adapter: parsing, quoted history, reply drafts, Gmail folders and UID cursors (fake IMAP)."""
from datetime import date
from email import message_from_bytes, policy

import pytest

from jobpipe.adapters.inbox.gmail_imap import GmailImap, ImapError, parse_list
from jobpipe.adapters.inbox.mail import build_reply, links_of, parse_email, strip_quoted

from .conftest import REPO

MAIL = REPO / "tests" / "fixtures" / "mail"


def raw(name: str) -> bytes:
    return (MAIL / name).read_bytes()


def test_parse_multipart_prefers_plain_and_keeps_links_and_threading():
    m = parse_email(raw("screening.eml"), thread_key="777")
    assert m.from_addr == "jane@acme.example" and m.from_name == "Jane Recruiter"
    assert m.ext_id == "<scr-1@acme.example>" and m.thread_key == "777"
    assert "What is your notice period?" in m.text
    assert "I applied yesterday" not in m.text and "wrote:" not in m.text      # quoted history removed
    assert ("https://acme.example/careers", "Acme careers") in m.links
    assert m.ts == "2026-11-25T10:00:00Z" and not m.auto


def test_encoded_headers_and_bulk_detection():
    inv = parse_email(raw("invite_ru.eml"))
    assert inv.from_name == "Мария" and "Gamma" in inv.subject
    alert = parse_email(raw("hh_alert.eml"))
    assert alert.auto                                   # List-Id: a machine sent it
    assert any("/vacancy/100000001" in h for h, _ in alert.links)


def test_owner_reply_threading_headers():
    m = parse_email(raw("owner_reply.eml"))
    assert m.in_reply_to == "<scr-1@acme.example>" and m.references == ["<scr-1@acme.example>"]


@pytest.mark.parametrize("text,kept,gone", [
    ("Sure, Tuesday works.\n\nOn Mon, 1 Dec 2026 at 10:00, Jane <j@a.example> wrote:\n> When?", "Tuesday", "When"),
    ("Подойдёт вторник.\n\nпн, 1 дек. 2026 г. в 10:00, Мария <m@g.example>:\n> Когда?", "вторник", "Когда"),
    ("Thanks!\n-----Original Message-----\nFrom: X\nold text", "Thanks", "old text"),
])
def test_strip_quoted(text, kept, gone):
    out = strip_quoted(text)
    assert kept in out and gone not in out


def test_links_of_survives_broken_html():
    assert links_of('<a href="https://x.example/1">One</a><a href="/2">Two') == [("https://x.example/1", "One")]


def test_build_reply_is_threaded_and_unsent_draft():
    b = build_reply(from_name="Alex Example", from_addr="alex@example.com", to_addr="jane@acme.example",
                    subject="C++ Engineer at Acme", body="Hello Jane,\n\nThanks.", in_reply_to="<scr-1@acme.example>",
                    references=["<root@acme.example>"])
    m = message_from_bytes(b, policy=policy.default)
    assert m["Subject"] == "Re: C++ Engineer at Acme" and m["To"] == "jane@acme.example"
    assert m["In-Reply-To"] == "<scr-1@acme.example>"
    assert m["References"] == "<root@acme.example> <scr-1@acme.example>"
    assert "Thanks." in m.get_content()
    again = build_reply(from_name="", from_addr="a@b.example", to_addr="c@d.example", subject="RE: x", body="y",
                        in_reply_to=None, references=[])
    assert b"Subject: RE: x" in again and b"In-Reply-To" not in again


def test_parse_list_finds_localized_special_folders():
    lines = [b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasChildren \\Noselect) "/" "[Gmail]"',
             b'(\\HasNoChildren \\Sent) "/" "[Gmail]/&BB4EQgQ,BEAEMAQyBDsENQQ9BD0ESwQ1-"',
             b'(\\Drafts \\HasNoChildren) "/" "[Gmail]/&BCcENQRABD0EPgQyBDgEOgQ4-"',
             b'(\\All \\HasNoChildren) "/" "[Gmail]/All Mail"']
    f = parse_list(lines)
    assert f["inbox"] == "INBOX" and f["sent"].startswith('"[Gmail]/&BB4E')
    assert f["drafts"].startswith('"[Gmail]/&BCc') and f["all"] == '"[Gmail]/All Mail"'


class FakeIMAP:
    """Just enough of imaplib.IMAP4 for GmailImap: UIDs 1..n in INBOX, flags never touched."""

    def __init__(self, messages: dict[int, bytes], validity=11, login_ok=True):
        self.messages = messages
        self.validity = validity
        self.login_ok = login_ok
        self.selected = None
        self.appended = []
        self.calls = []

    def login(self, user, password):
        import imaplib

        if not self.login_ok:
            raise imaplib.IMAP4.error(b"[AUTHENTICATIONFAILED] Invalid credentials")
        return "OK", [b"ok"]

    def logout(self):
        return "BYE", []

    def list(self):
        return "OK", [b'(\\HasNoChildren) "/" "INBOX"', b'(\\Sent) "/" "[Gmail]/Sent Mail"',
                      b'(\\Drafts) "/" "[Gmail]/Drafts"']

    def select(self, name, readonly=False):
        self.calls.append(("select", name, readonly))
        self.selected = name
        return "OK", [str(len(self.messages)).encode()]

    def response(self, code):
        return code, [str(self.validity).encode()]

    def uid(self, command, *args):
        self.calls.append((command, *args))
        if command == "search":
            crit = args[-1]
            if crit.startswith("UID "):
                lo = int(crit.split()[1].split(":")[0])
                uids = [u for u in self.messages if u >= lo] or [max(self.messages)]
            elif crit.startswith("SINCE"):
                uids = [u for u in self.messages if u >= 2]          # pretend uid 1 is older than the window
            else:
                uids = list(self.messages)
            return "OK", [" ".join(map(str, sorted(uids))).encode()]
        if command == "fetch":
            assert "BODY.PEEK[]" in args[1]                          # never marks mail as read
            out = []
            for u in map(int, args[0].split(",")):
                out.append((f"{u} (UID {u} X-GM-THRID 99{u} BODY[] {{10}}".encode(), self.messages[u]))
                out.append(b")")
            return "OK", out
        raise AssertionError(command)

    def append(self, mailbox, flags, date_time, message):
        self.appended.append((mailbox, flags, message))
        return "OK", [b"APPEND completed"]


def test_fetch_new_first_run_window_then_cursor_then_uidvalidity_reset():
    fake = FakeIMAP({1: b"a", 2: b"b", 3: b"c"})
    with GmailImap("u@example.com", "pw", factory=lambda host: fake) as mb:
        got, cur = mb.fetch_new("inbox", None, since=date(2026, 11, 12), limit=10)
        assert [b for b, _ in got] == [b"b", b"c"] and got[0][1] == "992"
        assert cur == {"uidvalidity": 11, "last_uid": 3}
        assert ("select", '"INBOX"', True) in fake.calls            # read-only
        got, cur2 = mb.fetch_new("inbox", cur, since=date(2026, 11, 12), limit=10)
        assert got == [] and cur2 == cur                              # "UID 4:*" returns uid 3 again: filtered
        fake.messages[4] = b"d"
        got, cur3 = mb.fetch_new("inbox", cur, since=date(2026, 11, 12), limit=10)
        assert [b for b, _ in got] == [b"d"] and cur3["last_uid"] == 4
        fake.validity = 12                                            # mailbox rebuilt: back to the window
        got, cur4 = mb.fetch_new("inbox", cur3, since=date(2026, 11, 12), limit=2)
        assert [b for b, _ in got] == [b"b", b"c"] and cur4 == {"uidvalidity": 12, "last_uid": 3}


def test_first_run_with_empty_window_starts_at_newest_uid():
    fake = FakeIMAP({1: b"a"})
    with GmailImap("u", "pw", factory=lambda host: fake) as mb:
        got, cur = mb.fetch_new("inbox", None, since=date(2026, 11, 12), limit=10)
    assert got == [] and cur["last_uid"] == 1


def test_append_draft_goes_to_the_drafts_folder_with_the_draft_flag():
    fake = FakeIMAP({1: b"a"})
    with GmailImap("u", "pw", factory=lambda host: fake) as mb:
        mb.append_draft(b"Subject: Re: x\r\n\r\nhi")
    assert fake.appended == [('"[Gmail]/Drafts"', r"(\Draft)", b"Subject: Re: x\r\n\r\nhi")]


def test_login_failure_is_reported_without_the_password():
    fake = FakeIMAP({}, login_ok=False)
    with pytest.raises(ImapError) as e, GmailImap("u", "secret-app-pw", factory=lambda host: fake):
        pass
    assert "secret-app-pw" not in str(e.value) and "login failed" in str(e.value)
