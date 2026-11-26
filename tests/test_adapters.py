"""HTTP adapters against httpx.MockTransport: no network."""
import json

import httpx
import pytest

from jobpipe.adapters.llm.openai_compat import OpenAICompatTransport, parse_retry_after
from jobpipe.adapters.notifier.telegram import TelegramNotifier
from jobpipe.config.settings import LLMProvider
from jobpipe.ports.llm import LLMHTTPError, LLMRequest, LLMTimeout

P = LLMProvider(base_url="https://llm.example/v1/", model="m1", key_env="K")
REQ = LLMRequest(model="m1", messages=[{"role": "user", "content": "hi"}], json_mode=True)


def transport_for(handler):
    return OpenAICompatTransport(httpx.Client(transport=httpx.MockTransport(handler)))


def test_openai_compat_success_and_request_shape():
    seen = {}

    def handler(request: httpx.Request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}],
                                         "usage": {"prompt_tokens": 7, "completion_tokens": 2}})

    resp = transport_for(handler).complete(P, "secret", REQ)
    assert resp.text == "{}" and resp.tokens_in == 7 and resp.tokens_out == 2
    assert seen["url"] == "https://llm.example/v1/chat/completions"
    assert seen["auth"] == "Bearer secret"
    assert seen["body"]["response_format"] == {"type": "json_object"}
    assert seen["body"]["model"] == "m1"


def test_openai_compat_429_with_retry_after():
    def handler(request):
        return httpx.Response(429, headers={"retry-after": "17"}, json={"error": {"message": "quota"}})

    with pytest.raises(LLMHTTPError) as e:
        transport_for(handler).complete(P, "k", REQ)
    assert e.value.status == 429 and e.value.retry_after_s == 17 and "quota" in e.value.message


def test_openai_compat_error_list_body():
    def handler(request):
        return httpx.Response(500, json=[{"error": {"message": "internal"}}])

    with pytest.raises(LLMHTTPError) as e:
        transport_for(handler).complete(P, "k", REQ)
    assert e.value.status == 500 and e.value.message == "internal"


def test_openai_compat_timeout():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(LLMTimeout):
        transport_for(handler).complete(P, "k", REQ)


def test_openai_compat_json_mode_off_for_provider():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "x"}}]})

    transport_for(handler).complete(P.model_copy(update={"json_mode": False}), "k", REQ)
    assert "response_format" not in seen["body"]


def test_parse_retry_after():
    assert parse_retry_after("5") == 5
    assert parse_retry_after(None) is None
    assert parse_retry_after("garbage") is None


def _notifier(handler):
    return TelegramNotifier("123:abc", 42, httpx.Client(transport=httpx.MockTransport(handler)),
                            sleep=lambda s: None)


def test_telegram_send_ok():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True})

    assert _notifier(handler).send("hello", silent=True)
    assert seen["body"]["chat_id"] == 42 and seen["body"]["disable_notification"] is True


def test_telegram_429_then_ok():
    replies = iter([httpx.Response(429, json={"parameters": {"retry_after": 1}}), httpx.Response(200, json={})])
    assert _notifier(lambda r: next(replies)).send("x")


def test_telegram_400_not_retried():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(400, json={"description": "chat not found"})

    assert not _notifier(handler).send("x")
    assert len(calls) == 1


def test_telegram_long_message_truncated():
    seen = {}

    def handler(request):
        seen["text"] = json.loads(request.content)["text"]
        return httpx.Response(200, json={})

    _notifier(handler).send("a" * 5000)
    assert len(seen["text"]) <= 4000
