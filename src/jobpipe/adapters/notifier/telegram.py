"""Owner notifications through the Telegram Bot API, for oneshot jobs (ADR 0013).

The bot token is part of the request URL, so errors are reported without the URL.
"""
from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Callable

import httpx

from ...ports.notifier import Buttons

log = logging.getLogger(__name__)

MAX_LEN = 4000  # Telegram's limit is 4096 characters


class TelegramNotifier:
    def __init__(self, token: str, chat_id: int, client: httpx.Client | None = None,
                 sleep: Callable[[float], None] = time.sleep, retries: int = 2):
        self._url = f"https://api.telegram.org/bot{token}/sendMessage"
        self.chat_id = chat_id
        self.client = client or httpx.Client(timeout=15)
        self.sleep = sleep
        self.retries = retries

    def send(self, text: str, *, silent: bool = False, buttons: Buttons | None = None) -> bool:
        if len(text) > MAX_LEN:
            text = text[: MAX_LEN - 20] + "\n…(обрезано)"
        body = {"chat_id": self.chat_id, "text": text, "disable_web_page_preview": True,
                "disable_notification": silent}
        if buttons:
            body["reply_markup"] = {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row]
                                                        for row in buttons]}
        for attempt in range(self.retries + 1):
            try:
                resp = self.client.post(self._url, json=body)
            except httpx.HTTPError as e:
                err = type(e).__name__
                retry_after = 2.0 * (attempt + 1)
            else:
                if resp.status_code == 200:
                    return True
                err = f"HTTP {resp.status_code}"
                retry_after = 2.0 * (attempt + 1)
                if resp.status_code == 429:
                    with contextlib.suppress(ValueError, KeyError, TypeError):
                        retry_after = float(resp.json()["parameters"]["retry_after"])
                elif resp.status_code < 500:
                    with contextlib.suppress(ValueError):
                        err += f": {resp.json().get('description', '')[:200]}"
                    log.error("telegram send failed", extra={"data": {"error": err}})
                    return False
            if attempt < self.retries:
                self.sleep(min(retry_after, 30))
        log.error("telegram send failed", extra={"data": {"error": err}})
        return False


class LogNotifier:
    """Fallback when Telegram is not configured: write the message to the log."""

    def send(self, text: str, *, silent: bool = False, buttons: Buttons | None = None) -> bool:
        log.warning("notification (telegram not configured)", extra={"data": {"text": text[:500]}})
        return False


class RecordingNotifier:
    """Test double."""

    def __init__(self) -> None:
        self.messages: list[str] = []
        self.buttons: list[Buttons | None] = []

    def send(self, text: str, *, silent: bool = False, buttons: Buttons | None = None) -> bool:
        self.messages.append(text)
        self.buttons.append(buttons)
        return True
