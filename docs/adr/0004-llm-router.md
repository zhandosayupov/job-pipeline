# 0004. LLM router

Date: 2026-11-26 · Status: accepted · Revised 2026-11-26: models per provider

## Context
Free tiers have small, changing limits; providers fail or rate-limit. The owner has no paid key yet.
On the first deploy, Gemini's free tier answered `503 high demand` or not at all for
`gemini-flash-latest` while `gemini-flash-lite-latest` answered in about 4 s on the same key: one
provider can be healthy for one model and unusable for another.

## Decision
A single `Router` serves every LLM task:
* `llm.yaml` lists providers (base URL, key env var, time zone their daily quota resets in,
  `trains_on_data`, timeout) and, for each provider, its **models in order of preference**, each with
  its own RPM/RPD/TPM/TPD and optional timeout (provider-level values are the defaults). A provider
  with a single `model:` is still accepted.
* A task's chain lists `provider` (all its enabled models, in order) or `provider/model-id` (one
  model). The chain is expanded into (provider, model) targets and tried in that order.
* Quotas are counted from `llm_calls` per (provider, model); a target without quota left is skipped.
* Cooldown and breaker state are kept per target (`provider_state` keyed `provider/model`):
  429 sets a cooldown from `retry-after` (or 60 s) and moves on; 5xx or timeout gets one retry after
  backoff, then the next target; `breaker_threshold` consecutive failures open the breaker for
  `breaker_minutes`. A provider-level problem (disabled, no key, trains on data for a personal
  request) skips all its models and is reported once.
* Structured output is validated with pydantic; one repair prompt (field paths and error types only,
  never values), then the next target.
* Every call, successful or not, is written to `llm_calls` with provider, model and prompt version.
* Most providers expose an OpenAI-compatible Chat Completions API, so one adapter covers Gemini,
  Groq, Cerebras, OpenRouter and Mistral. An Anthropic adapter (with Message Batches) comes when a
  key exists.
* Personal requests (CV, messages) skip `trains_on_data` providers unless the owner allows it, and
  are pseudonymized when `pii_redaction: required` (ADR 0008).

* `quota_scope: provider` counts one quota across all of a provider's models against the
  provider-level limits, for providers that limit the whole key rather than each model. A 503
  "high demand" is a model's capacity, not the key's quota, so trying the next model still helps.
* Chain entries are validated when the config loads (unknown provider or model fails at startup),
  and overlapping entries (`g` plus `g/big`) call each model once.

## Consequences
Provider and model limits in config are maintained by the owner; the router never assumes a
provider's limits. Model ids change: list them with `GET {base_url}/models` before editing.
Retry budget: each target gets one retry on 5xx or timeout, so a chain of N targets can take about
2N x `timeout_s` in the worst case. Fine for background tasks; an interactive path (a bot reply)
should use a short chain and short timeouts. Idle `provider_state` rows are removed by retention.
