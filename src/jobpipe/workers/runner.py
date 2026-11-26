"""Queue runner: `jobpipe run queue`, started by a timer. Drains due tasks, then exits."""
from __future__ import annotations

import logging
import os
import signal
import socket
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..clock import Clock
from ..config.settings import AppConfig
from ..domain.errors import Paused, Permanent
from ..ports.notifier import Notifier
from ..services import control
from ..services.queue import Queue, Task

log = logging.getLogger(__name__)


class Stopped(BaseException):
    """Raised inside the running handler when SIGTERM arrives (kill switch)."""


@dataclass
class HandlerContext:
    conn: sqlite3.Connection
    clock: Clock
    cfg: AppConfig
    notifier: Notifier
    side_effects: control.SideEffects
    services: dict[str, Any] = field(default_factory=dict)

    def guard(self) -> None:
        """Call before any external side effect."""
        control.ensure_not_paused(self.conn)


Handler = Callable[[Task, HandlerContext], None]


@dataclass
class RunSummary:
    status: str = "ok"
    done: int = 0
    retried: int = 0
    dead: int = 0
    unknown_side_effects: list[str] = field(default_factory=list)


class Runner:
    def __init__(self, conn: sqlite3.Connection, clock: Clock, cfg: AppConfig, handlers: dict[str, Handler],
                 notifier: Notifier, services: dict[str, Any] | None = None):
        self.conn = conn
        self.clock = clock
        self.cfg = cfg
        self.handlers = handlers
        self.notifier = notifier
        self.services = services or {}
        self.queue = Queue(conn, clock, cfg.settings.queue)
        self._stop = False

    def _on_sigterm(self, signum, frame) -> None:  # pragma: no cover - exercised in a subprocess test
        self._stop = True
        raise Stopped()

    def run(self, max_seconds: float | None = None, types: list[str] | None = None) -> RunSummary:
        summary = RunSummary()
        control.reap_stale_runs(self.conn, self.clock)
        if control.is_paused(self.conn):
            log.info("paused; queue run skipped")
            summary.status = "paused"
            return summary
        run_id = control.start_run(self.conn, self.clock, "run queue")
        owner = f"{socket.gethostname()}:{os.getpid()}:{run_id}"
        side_effects = control.SideEffects(self.conn, self.clock, run_id=run_id)
        ctx = HandlerContext(self.conn, self.clock, self.cfg, self.notifier, side_effects, self.services)
        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGTERM, self._on_sigterm)
        deadline = time.monotonic() + (max_seconds or self.cfg.settings.runner.max_seconds)
        final = "ok"
        try:
            unknown = side_effects.orphaned_to_unknown()
            if unknown:
                summary.unknown_side_effects = unknown
                self.notifier.send(
                    "Прерванные внешние действия, исход неизвестен (повторять не буду, нужно твоё решение):\n"
                    + "\n".join(f"• {k}" for k in unknown)
                )
            self.queue.reclaim_expired()
            while not self._stop and time.monotonic() < deadline:
                if control.is_paused(self.conn):
                    final = "stopped"
                    break
                task = self.queue.lease(owner, types)
                if task is None:
                    break
                self._run_one(task, ctx, summary)
        except Stopped:
            final = "stopped"
        except Exception:
            final = "failed"
            log.exception("queue run crashed")
            raise
        finally:
            summary.status = final if summary.status == "ok" else summary.status
            control.finish_run(self.conn, self.clock, run_id, final,
                               {"done": summary.done, "retried": summary.retried, "dead": summary.dead})
        return summary

    def _run_one(self, task: Task, ctx: HandlerContext, summary: RunSummary) -> None:
        handler = self.handlers.get(task.type)
        if handler is None:
            self._failed(task, f"no handler for task type {task.type!r}", summary, permanent=True)
            return
        try:
            handler(task, ctx)
        except Stopped:
            self.queue.release(task.id)
            raise
        except Paused:
            self.queue.release(task.id)
            self._stop = True
            return
        except Permanent as e:
            self._failed(task, f"Permanent: {e}", summary, permanent=True)
            return
        except Exception as e:
            self._failed(task, f"{type(e).__name__}: {e}", summary)
            return
        self.queue.complete(task.id)
        summary.done += 1
        log.info("task done", extra={"data": {"task": task.id, "type": task.type}})

    def _failed(self, task: Task, error: str, summary: RunSummary, *, permanent: bool = False) -> None:
        outcome = self.queue.fail(task.id, error, permanent=permanent)
        if outcome.dead:
            summary.dead += 1
            log.error("task dead-lettered", extra={"data": {"task": task.id, "type": task.type, "error": error}})
            self.notifier.send(
                f"Задача #{task.id} ({task.type}) ушла в dead-letter, попыток: {task.attempts + 1}.\n"
                f"Последняя ошибка: {error[:500]}"
            )
        else:
            summary.retried += 1
            log.warning("task failed, will retry",
                        extra={"data": {"task": task.id, "type": task.type, "retry_in_s": outcome.retry_in_s}})
