from __future__ import annotations

from typing import Protocol

# Inline keyboard: rows of (label, callback_data). callback_data is at most 64 bytes (Telegram).
Buttons = list[list[tuple[str, str]]]


class Notifier(Protocol):
    def send(self, text: str, *, silent: bool = False, buttons: Buttons | None = None) -> bool:
        """Deliver a message to the owner. Returns False if it could not be delivered."""
        ...
