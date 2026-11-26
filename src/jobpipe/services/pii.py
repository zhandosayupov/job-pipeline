"""Pseudonymization of personal values before LLM calls (ADR 0008).

Configured as {"NAME": ["Full Name", "Short Name"], "EMAIL": ["..."]}. Every variant becomes
`{{NAME}}`; restoring puts back the first (canonical) value.
"""
from __future__ import annotations

import re


class Pseudonymizer:
    def __init__(self, values: dict[str, list[str]]):
        pairs: list[tuple[str, str]] = []
        self._restore: dict[str, str] = {}
        for label, variants in values.items():
            clean = [v for v in variants if v and v.strip()]
            if not clean:
                continue
            placeholder = "{{" + label.upper() + "}}"
            self._restore[placeholder] = clean[0]
            pairs += [(v, placeholder) for v in clean]
        pairs.sort(key=lambda p: len(p[0]), reverse=True)
        self._pairs = [(re.compile(re.escape(v), re.IGNORECASE), ph) for v, ph in pairs]

    def redact(self, text: str) -> str:
        for pattern, placeholder in self._pairs:
            text = pattern.sub(placeholder, text)
        return text

    def restore(self, text: str) -> str:
        for placeholder, value in self._restore.items():
            text = text.replace(placeholder, value)
        return text

    def __bool__(self) -> bool:
        return bool(self._pairs)
