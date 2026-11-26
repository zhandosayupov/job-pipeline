# 0004. LLM router

Date: 2026-11-26 · Status: accepted

## Context
Free tiers have small, changing limits; providers fail or rate-limit. The owner has no paid key yet.

## Decision
A single `Router` serves every LLM task:
* `llm.yaml` lists providers (base URL, model, key env var, limits RPM/RPD/TPM/TPD, the time zone
  their daily quota resets in, `trains_on_data`) and per-task chains with a prompt version.
* Quotas are counted from `llm_calls`; a provider without quota left is skipped.
* 429: `provider_state.cooldown_until` from `retry-after` (or 60 s), next provider.
  5xx or timeout: one retry after backoff, then next provider.
  `breaker_threshold` consecutive failures open the breaker for `breaker_minutes`.
* Structured output is validated with pydantic; one repair prompt, then the next provider.
* Every call, successful or not, is written to `llm_calls` with the prompt version.
* Most providers expose an OpenAI-compatible Chat Completions API, so one adapter covers Gemini,
  Groq, Cerebras, OpenRouter and Mistral. An Anthropic adapter (with Message Batches) comes when a
  key exists.
* Personal requests (CV, messages) skip `trains_on_data` providers unless the owner allows it, and
  are pseudonymized when `pii_redaction: required` (ADR 0008).

## Consequences
Provider limits in config are maintained by the owner; the router never assumes a provider's limits.
