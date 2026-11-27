"""Gmail over IMAP with an app password (ADR 0010). Reads with UID cursors, writes drafts with APPEND.

Never sends mail and never changes flags: messages are fetched with BODY.PEEK (they stay unread) and
mailboxes are selected read-only. Folder names come from the IMAP special-use flags (\\Sent,
\\Drafts), because Gmail localizes them ("[Gmail]/Отправленные").
"""
from __future__ import annotations

import contextlib
import imaplib
import re
import time
from collections.abc import Callable
from datetime import date

_LIST = re.compile(r'^\((?P<flags>[^)]*)\)\s+(?:"[^"]*"|NIL)\s+(?P<name>.+)$')
_UID = re.compile(rb"UID (\d+)")
_THRID = re.compile(rb"X-GM-THRID (\d+)")


class ImapError(Exception):
    pass


def parse_list(lines: list[bytes]) -> dict[str, str]:
    """{"inbox": ..., "sent": ..., "drafts": ...} from LIST responses (names stay IMAP-encoded)."""
    out = {"inbox": "INBOX"}
    for raw in lines:
        if not raw:
            continue
        m = _LIST.match(raw.decode("ascii", "replace") if isinstance(raw, bytes) else raw)
        if not m:
            continue
        flags = m.group("flags").lower().split()
        name = m.group("name").strip()
        for flag, key in (("\\sent", "sent"), ("\\drafts", "drafts"), ("\\all", "all")):
            if flag in flags:
                out[key] = name
    return out


def _quote(name: str) -> str:
    return name if name.startswith('"') else f'"{name}"'


class GmailImap:
    """Context manager: `with GmailImap(user, password) as mb: mb.fetch_new(...)`."""

    def __init__(self, user: str, password: str, host: str = "imap.gmail.com",
                 factory: Callable[..., imaplib.IMAP4] | None = None, timeout: float = 60):
        self.user = user
        self._password = password
        self.host = host
        self.factory = factory or (lambda host: imaplib.IMAP4_SSL(host, 993, timeout=timeout))
        self.conn: imaplib.IMAP4 | None = None
        self._folders: dict[str, str] | None = None

    def __enter__(self) -> GmailImap:
        self.conn = self.factory(self.host)
        try:
            self.conn.login(self.user, self._password)
        except imaplib.IMAP4.error as e:
            raise ImapError(f"login failed: {str(e)[:120]}") from None
        return self

    def __exit__(self, *exc) -> None:
        if self.conn is not None:
            with contextlib.suppress(Exception):
                self.conn.logout()
            self.conn = None

    def _ok(self, typ: str, data, what: str):
        if typ != "OK":
            raise ImapError(f"{what}: {typ} {str(data)[:120]}")
        return data

    def folders(self) -> dict[str, str]:
        if self._folders is None:
            self._folders = parse_list(self._ok(*self.conn.list(), "LIST"))
        return self._folders

    def fetch_new(self, folder: str, cursor: dict | None, *, since: date, limit: int
                  ) -> tuple[list[tuple[bytes, str | None]], dict]:
        """Messages newer than the cursor (or since `since` on the first run / after UIDVALIDITY
        changed), oldest first, at most `limit`. Returns ([(raw, gmail thread id)], new cursor)."""
        name = self.folders().get(folder)
        if name is None:
            return [], cursor or {}
        self._ok(*self.conn.select(_quote(name), readonly=True), f"SELECT {folder}")
        validity = (self.conn.response("UIDVALIDITY")[1] or [b"0"])[0]
        validity = int(validity) if validity else 0
        if not cursor or cursor.get("uidvalidity") != validity:
            data = self._ok(*self.conn.uid("search", None, f"SINCE {since.strftime('%d-%b-%Y')}"), "SEARCH")
            last = 0
        else:
            last = int(cursor.get("last_uid", 0))
            data = self._ok(*self.conn.uid("search", None, f"UID {last + 1}:*"), "SEARCH")
        uids = sorted(int(u) for u in (data[0] or b"").split() if int(u) > last)[:limit]
        out: list[tuple[bytes, str | None]] = []
        new_last = last
        for i in range(0, len(uids), 25):
            chunk = uids[i:i + 25]
            resp = self._ok(*self.conn.uid("fetch", ",".join(map(str, chunk)), "(UID X-GM-THRID BODY.PEEK[])"),
                            "FETCH")
            for item in resp:
                if not isinstance(item, tuple) or len(item) < 2:
                    continue
                head, body = item[0], item[1]
                m = _UID.search(head)
                if not m:
                    continue
                t = _THRID.search(head)
                out.append((body, t.group(1).decode() if t else None))
                new_last = max(new_last, int(m.group(1)))
        if not uids and last == 0:
            # first run with nothing in the window: start from the newest UID so far
            data = self._ok(*self.conn.uid("search", None, "ALL"), "SEARCH")
            all_uids = [int(u) for u in (data[0] or b"").split()]
            new_last = max(all_uids, default=0)
        return out, {"uidvalidity": validity, "last_uid": new_last}

    def append_draft(self, raw: bytes) -> None:
        name = self.folders().get("drafts")
        if name is None:
            raise ImapError("no \\Drafts folder (is IMAP enabled for this Gmail account?)")
        self._ok(*self.conn.append(_quote(name), r"(\Draft)", imaplib.Time2Internaldate(time.time()), raw),
                 "APPEND")
