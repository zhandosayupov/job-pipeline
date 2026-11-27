"""E-mail parsing and reply drafts (stdlib `email`). Pure: bytes in, InMessage / bytes out."""
from __future__ import annotations

import re
from datetime import UTC, datetime
from email import message_from_bytes, policy
from email.message import EmailMessage
from email.utils import format_datetime, getaddresses, make_msgid, parseaddr, parsedate_to_datetime
from html.parser import HTMLParser

from ...domain.message import InMessage
from ...services.text import html_to_text

# Where the quoted history of a reply starts: "On Mon, ... wrote:", "-----Original Message-----", or a
# Russian Gmail/Outlook header ("пн, 1 дек. 2026 г. в 10:00, Имя <a@b>:"), then everything after it.
_QUOTE_START = re.compile(
    r"^\s*(?:On .{5,200}wrote:\s*$|-{2,}\s*Original Message\s*-{2,}|-{2,}\s*Forwarded message\s*-{2,}|"
    r"From: .+$|.{0,80}\d{4}\s*г\.\s*в\s*\d{1,2}:\d{2}.{0,200}:\s*$|"
    r"\S+,\s+\d{1,2}\s+\S+\s+\d{4}.{0,20}\d{1,2}:\d{2}.{0,120}:\s*$)", re.I | re.M)


class _Links(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._text = []

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            self.links.append((self._href, " ".join("".join(self._text).split())))
            self._href = None


def links_of(html: str) -> list[tuple[str, str]]:
    p = _Links()
    try:
        p.feed(html)
        p.close()
    except Exception:  # a broken newsletter must not stop the inbox
        pass
    return p.links


def strip_quoted(text: str) -> str:
    """The new part of a reply: drop `>` lines and everything from the quoted-history header on."""
    lines = [ln for ln in text.replace("\r\n", "\n").split("\n") if not ln.lstrip().startswith(">")]
    body = "\n".join(lines)
    m = _QUOTE_START.search(body)
    if m and m.start() > 0:
        body = body[: m.start()]
    return re.sub(r"\n{3,}", "\n\n", body).strip()


def _iso(date_header: str | None) -> str:
    try:
        dt = parsedate_to_datetime(date_header) if date_header else None
    except (TypeError, ValueError):
        dt = None
    if dt is None:
        dt = datetime.now(UTC)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _ids(value: str | None) -> list[str]:
    return re.findall(r"<[^<>\s]+>", value or "")


def parse_email(raw: bytes, *, thread_key: str | None = None, body_chars: int = 6000) -> InMessage:
    msg = message_from_bytes(raw, policy=policy.default)
    name, addr = parseaddr(str(msg.get("From", "")))
    plain = msg.get_body(preferencelist=("plain",))
    html_part = msg.get_body(preferencelist=("html",))
    html = ""
    if html_part is not None:
        try:
            html = html_part.get_content()
        except (LookupError, UnicodeDecodeError):
            html = html_part.get_payload(decode=True).decode("utf-8", "replace")
    text = ""
    if plain is not None:
        try:
            text = plain.get_content()
        except (LookupError, UnicodeDecodeError):
            text = plain.get_payload(decode=True).decode("utf-8", "replace")
    if not text.strip() and html:
        text = html_to_text(html)
    auto = bool(msg.get("Auto-Submitted", "no").lower() != "no" or
                msg.get("Precedence", "").lower() in ("bulk", "list", "junk") or msg.get("List-Id"))
    msg_id = (_ids(str(msg.get("Message-ID", ""))) or [f"<no-id-{hash(raw) & 0xffffffff:x}@jobpipe>"])[0]
    return InMessage(
        channel="gmail",
        ext_id=msg_id,
        ts=_iso(msg.get("Date")),
        from_addr=addr.lower(),
        from_name=name,
        to=[a.lower() for _, a in getaddresses([str(x) for x in msg.get_all("To", []) + msg.get_all("Cc", [])])
            if a],
        subject=" ".join(str(msg.get("Subject", "")).split()),
        text=strip_quoted(text)[:body_chars],
        links=links_of(html) if html else [],
        in_reply_to=(_ids(str(msg.get("In-Reply-To", ""))) or [None])[0],
        references=_ids(str(msg.get("References", ""))),
        thread_key=thread_key,
        auto=auto,
    )


def build_reply(*, from_name: str, from_addr: str, to_addr: str, subject: str, body: str,
                in_reply_to: str | None, references: list[str], now: datetime | None = None) -> bytes:
    """A reply as a draft (RFC 5322 bytes for IMAP APPEND). Threaded with In-Reply-To/References so
    Gmail files it in the same conversation."""
    m = EmailMessage()
    m["From"] = f"{from_name} <{from_addr}>" if from_name else from_addr
    m["To"] = to_addr
    m["Subject"] = subject if re.match(r"^\s*(re|ответ)\s*:", subject, re.I) else f"Re: {subject}"
    m["Date"] = format_datetime(now or datetime.now(UTC))
    m["Message-ID"] = make_msgid(domain=from_addr.rpartition("@")[2] or "jobpipe.local")
    if in_reply_to:
        m["In-Reply-To"] = in_reply_to
        m["References"] = " ".join([*[r for r in references if r != in_reply_to], in_reply_to])
    m.set_content(body)
    return m.as_bytes(policy=policy.SMTP)
