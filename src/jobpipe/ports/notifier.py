from __future__ import annotations

from typing import Protocol


class Notifier(Protocol):
    def send(self, text: str, *, silent: bool = False) -> bool:
        """Deliver a message to the owner. Returns False if it could not be delivered."""
        ...
