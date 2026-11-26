# 0001. One resident bot, everything else oneshot on timers

Date: 2026-11-26 · Status: accepted

## Context
The VM is an e2-micro with 1 GB RAM. The spec proposed three always-on services (core, LLM
worker, browser worker). Most work is periodic: collect every few hours, label once a day,
drain the queue every minute. Only the Telegram bot must react within seconds (`/pause`, buttons).

## Decision
* `jobpipe-bot.service` is the only resident process: Telegram bot (long polling), `/healthz`,
  the worker API (S4) and the kill switch.
* Every other job is a oneshot command (`jobpipe run queue`, `jobpipe run collect`, ...) started
  by a systemd timer. It records itself in `runs`, does its work, exits.
* Module boundaries stay as in the spec; process boundaries do not.

## Consequences
* Idle memory is ~60 MB instead of ~200 MB. Python start-up (~0.5 s) is paid on every run, which is
  fine at a one-minute cadence.
* Config is re-read on every oneshot run, so most config changes need no reload; the bot reloads on
  `/reload` (S1).
* Concurrency between runs is handled by task leases (ADR 0003), not by a long-lived scheduler.
