"""JSON logs on stderr with redaction of secrets, emails and phone numbers (ADR 0015)."""
from __future__ import annotations

import json
import logging
import re
import sys
from collections.abc import Iterable
from datetime import UTC, datetime

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# International numbers with a leading +, and KZ/RU local format 8 (7xx) xxx-xx-xx.
PHONE_RE = re.compile(
    r"(?<![\w+])\+\d[\d\s\-()]{7,}\d|(?<!\w)8[\s\-(]*\d{3}[\s\-)]*\d{3}[\s\-]\d{2}[\s\-]\d{2}(?!\d)"
)


class Redactor:
    def __init__(self, secret_values: Iterable[str] = ()):
        # Longest first so a secret containing another is masked whole.
        self._secrets = sorted({v for v in secret_values if v and len(v) >= 6}, key=len, reverse=True)

    def __call__(self, text: str) -> str:
        for s in self._secrets:
            text = text.replace(s, "[secret]")
        text = EMAIL_RE.sub("[email]", text)
        return PHONE_RE.sub("[phone]", text)


class JsonFormatter(logging.Formatter):
    def __init__(self, redactor: Redactor):
        super().__init__()
        self.redact = redactor

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        extra = getattr(record, "data", None)
        if isinstance(extra, dict):
            payload["data"] = extra
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return self.redact(json.dumps(payload, ensure_ascii=False, default=str))


def setup_logging(level: str = "INFO", secret_values: Iterable[str] = ()) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter(Redactor(secret_values)))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # httpx logs full request URLs at INFO; some providers put keys in query strings.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
