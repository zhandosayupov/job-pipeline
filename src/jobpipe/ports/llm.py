"""LLM transport port: one HTTP call to one provider. Routing lives in services.llm_router."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from ..config.settings import LLMProvider


@dataclass(frozen=True)
class LLMRequest:
    model: str
    messages: list[dict[str, str]]
    temperature: float = 0.2
    max_tokens: int = 1024
    json_mode: bool = False


@dataclass(frozen=True)
class LLMResponse:
    text: str
    tokens_in: int = 0
    tokens_out: int = 0


@dataclass
class LLMHTTPError(Exception):
    status: int
    message: str = ""
    retry_after_s: float | None = None
    body: str = field(default="", repr=False)

    def __str__(self) -> str:
        return f"HTTP {self.status}: {self.message[:200]}"


class LLMTimeout(Exception):
    pass


class LLMTransport(Protocol):
    def complete(self, provider: LLMProvider, api_key: str, request: LLMRequest) -> LLMResponse: ...
