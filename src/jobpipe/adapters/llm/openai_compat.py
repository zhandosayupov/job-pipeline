"""OpenAI-compatible Chat Completions adapter (Gemini, Groq, Cerebras, OpenRouter, Mistral)."""
from __future__ import annotations

from email.utils import parsedate_to_datetime

import httpx

from ...config.settings import LLMProvider
from ...ports.llm import LLMHTTPError, LLMRequest, LLMResponse, LLMTimeout

USER_AGENT = "jobpipe/0.1"


def parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        from datetime import UTC, datetime

        return max(0.0, (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds())
    except (TypeError, ValueError):
        return None


class OpenAICompatTransport:
    def __init__(self, client: httpx.Client | None = None):
        self.client = client or httpx.Client(headers={"User-Agent": USER_AGENT})

    def complete(self, provider: LLMProvider, api_key: str, request: LLMRequest) -> LLMResponse:
        body: dict = {
            "model": request.model,
            "messages": request.messages,
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
        }
        if request.json_mode and provider.json_mode:
            body["response_format"] = {"type": "json_object"}
        url = provider.base_url.rstrip("/") + "/chat/completions"
        try:
            resp = self.client.post(
                url, json=body, headers={"Authorization": f"Bearer {api_key}"}, timeout=provider.timeout_s
            )
        except httpx.TimeoutException as e:
            raise LLMTimeout(str(e)) from e
        except httpx.TransportError as e:
            raise LLMHTTPError(status=0, message=f"transport: {type(e).__name__}") from e
        if resp.status_code != 200:
            raise LLMHTTPError(
                status=resp.status_code,
                message=_error_message(resp),
                retry_after_s=parse_retry_after(resp.headers.get("retry-after")),
                body=resp.text[:2000],
            )
        try:
            data = resp.json()
            text = data["choices"][0]["message"]["content"] or ""
        except (ValueError, KeyError, IndexError, TypeError) as e:
            raise LLMHTTPError(status=502, message=f"malformed response: {e}", body=resp.text[:2000]) from e
        usage = data.get("usage") or {}
        return LLMResponse(
            text=text,
            tokens_in=int(usage.get("prompt_tokens") or 0),
            tokens_out=int(usage.get("completion_tokens") or 0),
        )


def _error_message(resp: httpx.Response) -> str:
    try:
        data = resp.json()
    except ValueError:
        return resp.text[:200]
    if isinstance(data, list) and data:
        data = data[0]
    if isinstance(data, dict):
        err = data.get("error", data)
        if isinstance(err, dict):
            return str(err.get("message") or err)[:200]
        return str(err)[:200]
    return str(data)[:200]
