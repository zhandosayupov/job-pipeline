"""A message as every inbox channel returns it (Gmail, Telegram userbot). Pure data."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Literal

Channel = Literal["gmail", "telegram"]


@dataclass
class InMessage:
    channel: Channel
    ext_id: str                        # Message-ID header, or "<chat id>:<message id>" for Telegram
    ts: str                            # ISO-8601 UTC
    from_addr: str                     # e-mail address, or "@username" / "tg:<user id>"
    from_name: str = ""
    to: list[str] = field(default_factory=list)
    subject: str = ""
    text: str = ""                     # plain text, quoted history removed
    links: list[tuple[str, str]] = field(default_factory=list)   # (href, anchor text) from the HTML part
    in_reply_to: str | None = None
    references: list[str] = field(default_factory=list)
    thread_key: str | None = None      # Gmail X-GM-THRID or Telegram chat id, when the channel gives one
    direction: Literal["in", "out"] = "in"
    auto: bool = False                 # Auto-Submitted / Precedence: bulk / List-Id: sent by a machine

    @property
    def id(self) -> str:
        return f"{self.channel}:{hashlib.sha1(self.ext_id.encode('utf-8')).hexdigest()[:20]}"

    @property
    def domain(self) -> str:
        return self.from_addr.rpartition("@")[2].lower() if "@" in self.from_addr else ""
