"""LLM router (ADR 0004): per-task provider chains with quotas, cooldowns, a circuit breaker,
structured-output validation with one repair, and a usage ledger."""
from __future__ import annotations

import json
import logging
import random
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ValidationError

from ..adapters.storage.db import tx
from ..clock import Clock, iso, parse_iso
from ..config.settings import LLMConfig, LLMProvider, Secrets
from ..ports.llm import LLMHTTPError, LLMRequest, LLMResponse, LLMTimeout, LLMTransport
from .pii import Pseudonymizer
from .prompts import PromptStore

log = logging.getLogger(__name__)


class LLMUnavailable(Exception):
    """No provider in the chain produced a valid answer."""

    def __init__(self, task: str, attempts: list[str]):
        self.task = task
        self.attempts = attempts
        super().__init__(f"{task}: no provider succeeded ({'; '.join(attempts) or 'chain empty'})")


@dataclass
class Completion:
    text: str
    data: BaseModel | None
    provider: str
    model: str
    prompt_version: str
    tokens_in: int
    tokens_out: int
    latency_ms: int
    attempts: list[str] = field(default_factory=list)


class _InvalidOutput(Exception):
    pass


def _parse(schema: type[BaseModel], text: str) -> BaseModel:
    raw = text.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw[raw.find("{"):] if "{" in raw else raw
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1:
        raise _InvalidOutput("no JSON object in reply")
    try:
        return schema.model_validate(json.loads(raw[start : end + 1]))
    except json.JSONDecodeError as e:
        raise _InvalidOutput(f"not valid JSON: {e.msg} at line {e.lineno} column {e.colno}") from e
    except ValidationError as e:
        raise _InvalidOutput(_describe(e)) from e


def _describe(e: ValidationError) -> str:
    """Field paths and error kinds only. Never the offending values: by now they may hold
    restored personal data, and this text is sent back to the provider in the repair prompt."""
    parts = [f"{'.'.join(map(str, err['loc'])) or '(root)'}: {err['type']}"
             for err in e.errors(include_input=False, include_url=False, include_context=False)]
    return "invalid fields: " + "; ".join(parts)[:300]


class Router:
    def __init__(
        self,
        conn: sqlite3.Connection,
        clock: Clock,
        cfg: LLMConfig,
        secrets: Secrets,
        transport: LLMTransport,
        prompts: PromptStore,
        *,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ):
        self.conn = conn
        self.clock = clock
        self.cfg = cfg
        self.secrets = secrets
        self.transport = transport
        self.prompts = prompts
        self.sleep = sleep
        self.rng = rng or random.Random()

    # --- public ------------------------------------------------------------------------

    def complete(
        self,
        task: str,
        variables: dict[str, object],
        *,
        schema: type[BaseModel] | None = None,
        personal: bool = False,
    ) -> Completion:
        if task not in self.cfg.tasks:
            raise ValueError(f"no LLM task {task!r} in llm.yaml")
        tcfg = self.cfg.tasks[task]
        prompt, messages = self.prompts.render(task, tcfg.prompt_version, variables)
        pseudo = None
        if personal and self.cfg.pii_redaction == "required":
            pseudo = Pseudonymizer(self.cfg.pii_values)
            messages = [{**m, "content": pseudo.redact(m["content"])} for m in messages]
        est_tokens = sum(len(m["content"]) for m in messages) // 4 + tcfg.max_tokens
        attempts: list[str] = []

        for name in tcfg.chain:
            provider = self.cfg.providers.get(name)
            reason = self._skip_reason(name, provider, personal, est_tokens)
            if reason:
                attempts.append(f"{name}: skipped ({reason})")
                continue
            assert provider is not None
            key = self.secrets.get(provider.key_env)
            assert key is not None
            request = LLMRequest(
                model=provider.model,
                messages=messages,
                temperature=tcfg.temperature,
                max_tokens=tcfg.max_tokens,
                json_mode=schema is not None,
            )
            result = self._try_provider(task, name, provider, key, request, prompt.version, schema, pseudo, attempts)
            if result is not None:
                result.attempts = attempts
                return result
        raise LLMUnavailable(task, attempts)

    # --- provider selection ------------------------------------------------------------

    def _skip_reason(self, name: str, provider: LLMProvider | None, personal: bool, est_tokens: int) -> str | None:
        if provider is None:
            return "not configured"
        if not provider.enabled:
            return "disabled"
        if not self.secrets.get(provider.key_env):
            return f"no key {provider.key_env}"
        if personal and provider.trains_on_data and not self.cfg.personal_on_training_providers:
            return "trains on data"
        now = iso(self.clock.now())
        st = self._state(name)
        if st["cooldown_until"] and st["cooldown_until"] > now:
            return f"cooldown until {st['cooldown_until']}"
        if st["breaker_until"] and st["breaker_until"] > now:
            return f"breaker open until {st['breaker_until']}"
        return self._quota_reason(name, provider, est_tokens)

    def _quota_reason(self, name: str, p: LLMProvider, est_tokens: int) -> str | None:
        now = self.clock.now()
        minute_ago = iso(now - timedelta(seconds=60))
        day_start = iso(self._day_start(now, p.day_reset_tz))
        q = "SELECT COUNT(*), COALESCE(SUM(tokens_in + tokens_out), 0) FROM llm_calls WHERE provider = ? AND ts >= ?"
        n_min, tok_min = self.conn.execute(q, (name, minute_ago)).fetchone()
        n_day, tok_day = self.conn.execute(q, (name, day_start)).fetchone()
        if p.rpm is not None and n_min + 1 > p.rpm:
            return "rpm quota"
        if p.rpd is not None and n_day + 1 > p.rpd:
            return "rpd quota"
        if p.tpm is not None and tok_min + est_tokens > p.tpm:
            return "tpm quota"
        if p.tpd is not None and tok_day + est_tokens > p.tpd:
            return "tpd quota"
        return None

    @staticmethod
    def _day_start(now: datetime, tz: str) -> datetime:
        local = now.astimezone(ZoneInfo(tz))
        return local.replace(hour=0, minute=0, second=0, microsecond=0)

    # --- calling -----------------------------------------------------------------------

    def _try_provider(
        self,
        task: str,
        name: str,
        provider: LLMProvider,
        key: str,
        request: LLMRequest,
        version: str,
        schema: type[BaseModel] | None,
        pseudo: Pseudonymizer | None,
        attempts: list[str],
    ) -> Completion | None:
        transient_retries = 1
        repaired = False
        while True:
            t0 = time.monotonic()
            try:
                resp: LLMResponse = self.transport.complete(provider, key, request)
            except LLMHTTPError as e:
                latency = int((time.monotonic() - t0) * 1000)
                if e.status == 429:
                    self._record(task, name, provider, version, "rate_limited", latency, error=str(e))
                    cooldown = e.retry_after_s if e.retry_after_s is not None else self.cfg.default_cooldown_s
                    self._set_cooldown(name, cooldown)
                    attempts.append(f"{name}: 429, cooldown {cooldown:.0f}s")
                    return None
                retryable = e.status == 0 or e.status >= 500
                self._record(task, name, provider, version, "error", latency, error=str(e))
                if retryable and transient_retries > 0:
                    transient_retries -= 1
                    self.sleep(self.cfg.retry_backoff_s * (1 + self.rng.random()))
                    continue
                self._failure(name)
                attempts.append(f"{name}: {e}")
                return None
            except LLMTimeout as e:
                latency = int((time.monotonic() - t0) * 1000)
                self._record(task, name, provider, version, "timeout", latency, error=str(e)[:200])
                if transient_retries > 0:
                    transient_retries -= 1
                    self.sleep(self.cfg.retry_backoff_s * (1 + self.rng.random()))
                    continue
                self._failure(name)
                attempts.append(f"{name}: timeout")
                return None

            latency = int((time.monotonic() - t0) * 1000)
            text = pseudo.restore(resp.text) if pseudo else resp.text
            data = None
            if schema is not None:
                try:
                    data = _parse(schema, text)
                except _InvalidOutput as e:
                    self._record(task, name, provider, version, "invalid_output", latency,
                                 resp.tokens_in, resp.tokens_out, error=str(e))
                    if not repaired:
                        repaired = True
                        request = LLMRequest(
                            model=request.model,
                            messages=[
                                *request.messages,
                                {"role": "assistant", "content": resp.text},
                                {"role": "user", "content": "That reply was not valid JSON for the required "
                                 f"format ({e}). Reply again with the corrected JSON object only."},
                            ],
                            temperature=request.temperature,
                            max_tokens=request.max_tokens,
                            json_mode=request.json_mode,
                        )
                        continue
                    self._failure(name)
                    attempts.append(f"{name}: invalid output after repair")
                    return None
            self._record(task, name, provider, version, "ok", latency, resp.tokens_in, resp.tokens_out)
            self._success(name)
            attempts.append(f"{name}: ok")
            return Completion(
                text=text,
                data=data,
                provider=name,
                model=provider.model,
                prompt_version=version,
                tokens_in=resp.tokens_in,
                tokens_out=resp.tokens_out,
                latency_ms=latency,
            )

    # --- ledger and provider state -----------------------------------------------------

    def _record(self, task: str, name: str, p: LLMProvider, version: str, status: str, latency_ms: int,
                tokens_in: int = 0, tokens_out: int = 0, error: str | None = None) -> None:
        with tx(self.conn):
            self.conn.execute(
                "INSERT INTO llm_calls(ts, task, provider, model, prompt_version, status, tokens_in, tokens_out, "
                "latency_ms, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (iso(self.clock.now()), task, name, p.model, version, status, tokens_in, tokens_out,
                 latency_ms, error),
            )
        if status != "ok":
            log.warning("llm call failed", extra={"data": {"task": task, "provider": name, "status": status,
                                                           "error": (error or "")[:200]}})

    def _state(self, name: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM provider_state WHERE provider = ?", (name,)).fetchone()
        if row is None:
            with tx(self.conn):
                self.conn.execute("INSERT OR IGNORE INTO provider_state(provider) VALUES (?)", (name,))
            row = self.conn.execute("SELECT * FROM provider_state WHERE provider = ?", (name,)).fetchone()
        return row

    def _set_cooldown(self, name: str, seconds: float) -> None:
        self._state(name)
        until = iso(self.clock.now() + timedelta(seconds=seconds))
        with tx(self.conn):
            self.conn.execute("UPDATE provider_state SET cooldown_until = ? WHERE provider = ?", (until, name))

    def _failure(self, name: str) -> None:
        st = self._state(name)
        n = st["consecutive_failures"] + 1
        breaker = st["breaker_until"]
        if n >= self.cfg.breaker_threshold:
            breaker = iso(self.clock.now() + timedelta(minutes=self.cfg.breaker_minutes))
            n = 0
            log.warning("llm breaker open", extra={"data": {"provider": name, "until": breaker}})
        with tx(self.conn):
            self.conn.execute(
                "UPDATE provider_state SET consecutive_failures = ?, breaker_until = ? WHERE provider = ?",
                (n, breaker, name),
            )

    def _success(self, name: str) -> None:
        self._state(name)
        with tx(self.conn):
            self.conn.execute(
                "UPDATE provider_state SET consecutive_failures = 0, breaker_until = NULL WHERE provider = ?",
                (name,),
            )

    # --- reporting ---------------------------------------------------------------------

    def usage_today(self, tz: str = "UTC") -> list[dict]:
        start = iso(self._day_start(self.clock.now(), tz))
        rows = self.conn.execute(
            "SELECT provider, COUNT(*) AS calls, SUM(status = 'ok') AS ok, "
            "COALESCE(SUM(tokens_in + tokens_out), 0) AS tokens FROM llm_calls WHERE ts >= ? GROUP BY provider",
            (start,),
        ).fetchall()
        return [dict(r) for r in rows]


def provider_status(conn: sqlite3.Connection, clock: Clock) -> dict[str, str]:
    """Human-readable cooldown/breaker state per provider, for /status."""
    now = iso(clock.now())
    out = {}
    for r in conn.execute("SELECT * FROM provider_state").fetchall():
        if r["breaker_until"] and r["breaker_until"] > now:
            out[r["provider"]] = f"breaker until {parse_iso(r['breaker_until']):%H:%M} UTC"
        elif r["cooldown_until"] and r["cooldown_until"] > now:
            out[r["provider"]] = f"cooldown until {parse_iso(r['cooldown_until']):%H:%M} UTC"
    return out
