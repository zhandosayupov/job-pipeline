from collections import deque

import pytest
from pydantic import BaseModel

from jobpipe.config.settings import LLMConfig, LLMProvider, LLMTask, Secrets
from jobpipe.ports.llm import LLMHTTPError, LLMResponse, LLMTimeout
from jobpipe.services.llm_router import LLMUnavailable, Router
from jobpipe.services.prompts import PromptStore


class Out(BaseModel):
    ok: bool
    echo: str


class ScriptedTransport:
    """Each provider gets a queue of outcomes: an LLMResponse, or an exception to raise."""

    def __init__(self, script):
        self.script = {k: deque(v) for k, v in script.items()}
        self.calls = []

    def complete(self, provider, api_key, request):
        name = next(n for n, p in PROVIDERS.items() if p.model == provider.model)
        self.calls.append((name, request))
        outcome = self.script[name].popleft()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


PROVIDERS = {
    "a": LLMProvider(base_url="https://a", model="ma", key_env="KA", rpm=100),
    "b": LLMProvider(base_url="https://b", model="mb", key_env="KB"),
    "t": LLMProvider(base_url="https://t", model="mt", key_env="KT", trains_on_data=True),
}
OK = LLMResponse('{"ok": true, "echo": "hi"}', 10, 5)


@pytest.fixture
def prompts(tmp_path):
    (tmp_path / "ping").mkdir()
    (tmp_path / "ping" / "v1.md").write_text("## system\nsys\n## user\nsay {{word}}\n", encoding="utf-8")
    return PromptStore(tmp_path)


def make(conn, clock, prompts, script, chain=("a", "b"), **cfg_kw):
    cfg = LLMConfig(
        providers=PROVIDERS,
        tasks={"ping": LLMTask(chain=list(chain), prompt_version="v1")},
        **cfg_kw,
    )
    transport = ScriptedTransport(script)
    sleeps = []
    router = Router(conn, clock, cfg, Secrets({"KA": "ka-key", "KB": "kb-key", "KT": "kt-key"}), transport,
                    prompts, sleep=sleeps.append)
    return router, transport, sleeps


def ledger(conn):
    return [tuple(r) for r in conn.execute("SELECT provider, status, prompt_version FROM llm_calls ORDER BY id")]


def test_success_on_first_provider(conn, clock, prompts):
    router, t, _ = make(conn, clock, prompts, {"a": [OK]})
    res = router.complete("ping", {"word": "hi"}, schema=Out)
    assert res.provider == "a" and res.data == Out(ok=True, echo="hi")
    assert t.calls[0][1].json_mode is True
    assert t.calls[0][1].messages[-1]["content"] == "say hi"
    assert ledger(conn) == [("a", "ok", "v1")]


def test_429_sets_cooldown_and_falls_through(conn, clock, prompts):
    router, _, _ = make(conn, clock, prompts, {"a": [LLMHTTPError(429, "slow down", retry_after_s=30)], "b": [OK]})
    assert router.complete("ping", {"word": "hi"}, schema=Out).provider == "b"
    # a is cooling down: the next call goes straight to b
    router2, t2, _ = make(conn, clock, prompts, {"a": [OK], "b": [OK]})
    assert router2.complete("ping", {"word": "hi"}).provider == "b"
    assert [c[0] for c in t2.calls] == ["b"]
    clock.advance(31)
    router3, _, _ = make(conn, clock, prompts, {"a": [OK]})
    assert router3.complete("ping", {"word": "hi"}).provider == "a"


def test_5xx_retried_once_then_succeeds(conn, clock, prompts):
    router, _, sleeps = make(conn, clock, prompts, {"a": [LLMHTTPError(503, "down"), OK]})
    assert router.complete("ping", {"word": "hi"}).provider == "a"
    assert len(sleeps) == 1
    assert [s for _, s, _ in ledger(conn)] == ["error", "ok"]


def test_timeouts_exhaust_then_next_provider(conn, clock, prompts):
    router, _, _ = make(conn, clock, prompts, {"a": [LLMTimeout("t"), LLMTimeout("t")], "b": [OK]})
    res = router.complete("ping", {"word": "hi"})
    assert res.provider == "b"
    assert res.attempts == ["a: timeout", "b: ok"]


def test_invalid_json_repaired(conn, clock, prompts):
    router, t, _ = make(conn, clock, prompts, {"a": [LLMResponse("sure! here it is"), OK]})
    res = router.complete("ping", {"word": "hi"}, schema=Out)
    assert res.provider == "a"
    repair_msgs = t.calls[1][1].messages
    assert repair_msgs[-2] == {"role": "assistant", "content": "sure! here it is"}
    assert "not valid JSON" in repair_msgs[-1]["content"]


def test_invalid_twice_moves_on(conn, clock, prompts):
    bad = LLMResponse('{"ok": "maybe"}')
    router, _, _ = make(conn, clock, prompts, {"a": [bad, bad], "b": [OK]})
    assert router.complete("ping", {"word": "hi"}, schema=Out).provider == "b"


def test_fenced_json_is_accepted(conn, clock, prompts):
    fenced = LLMResponse('```json\n{"ok": true, "echo": "hi"}\n```')
    router, _, _ = make(conn, clock, prompts, {"a": [fenced]})
    assert router.complete("ping", {"word": "hi"}, schema=Out).data.echo == "hi"


def test_breaker_opens_after_threshold(conn, clock, prompts):
    for _ in range(3):
        router, _, _ = make(conn, clock, prompts, {"a": [LLMHTTPError(401, "bad key")], "b": [OK]},
                            breaker_threshold=3, breaker_minutes=10)
        router.complete("ping", {"word": "hi"})
    router, _t, _ = make(conn, clock, prompts, {"a": [OK], "b": [OK]}, breaker_threshold=3)
    res = router.complete("ping", {"word": "hi"})
    assert res.provider == "b" and "breaker open" in res.attempts[0]
    clock.advance(601)
    router, _, _ = make(conn, clock, prompts, {"a": [OK]})
    assert router.complete("ping", {"word": "hi"}).provider == "a"


def test_rpm_quota_skips_provider(conn, clock, prompts):
    limited = dict(PROVIDERS)
    limited["a"] = PROVIDERS["a"].model_copy(update={"rpm": 1})
    cfg = LLMConfig(providers=limited, tasks={"ping": LLMTask(chain=["a", "b"])})
    t = ScriptedTransport({"a": [OK, OK], "b": [OK]})
    router = Router(conn, clock, cfg, Secrets({"KA": "x" * 8, "KB": "y" * 8}), t, prompts, sleep=lambda s: None)
    assert router.complete("ping", {"word": "hi"}).provider == "a"
    res = router.complete("ping", {"word": "hi"})
    assert res.provider == "b" and "rpm quota" in res.attempts[0]
    clock.advance(61)
    assert router.complete("ping", {"word": "hi"}).provider == "a"


def test_personal_skips_training_provider_and_redacts(conn, clock, prompts):
    router, t, _ = make(
        conn, clock, prompts,
        {"b": [LLMResponse('{"ok": true, "echo": "{{NAME}}"}')]},
        chain=("t", "b"),
        pii_values={"NAME": ["Jane Doe", "Jane"]},
    )
    res = router.complete("ping", {"word": "Jane Doe"}, schema=Out, personal=True)
    assert "trains on data" in res.attempts[0]
    assert t.calls[0][0] == "b"
    assert "Jane" not in t.calls[0][1].messages[-1]["content"]
    assert "{{NAME}}" in t.calls[0][1].messages[-1]["content"]
    assert res.data.echo == "Jane Doe"


def test_missing_key_is_skipped(conn, clock, prompts):
    cfg = LLMConfig(providers=PROVIDERS, tasks={"ping": LLMTask(chain=["a", "b"])})
    t = ScriptedTransport({"b": [OK]})
    router = Router(conn, clock, cfg, Secrets({"KB": "kb-key"}), t, prompts, sleep=lambda s: None)
    res = router.complete("ping", {"word": "hi"})
    assert res.provider == "b" and "no key KA" in res.attempts[0]


def test_all_fail_raises_unavailable(conn, clock, prompts):
    router, _, _ = make(conn, clock, prompts, {"a": [LLMHTTPError(400, "x")], "b": [LLMHTTPError(400, "y")]})
    with pytest.raises(LLMUnavailable) as e:
        router.complete("ping", {"word": "hi"})
    assert len(e.value.attempts) == 2


def test_unknown_task(conn, clock, prompts):
    router, _, _ = make(conn, clock, prompts, {})
    with pytest.raises(ValueError):
        router.complete("nope", {})
