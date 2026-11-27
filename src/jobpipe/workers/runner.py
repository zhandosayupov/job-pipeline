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


class Stopped(Exception):
    """Raised by ctx.guard() after SIGTERM: the kill switch asked this run to stop."""


@dataclass
class HandlerContext:
    conn: sqlite3.Connection
    clock: Clock
    cfg: AppConfig
    notifier: Notifier
    side_effects: control.SideEffects
    services: dict[str, Any] = field(default_factory=dict)
    stop_requested: Callable[[], bool] = lambda: False

    def guard(self) -> None:
        """Call before any external side effect and inside long loops. Raises Stopped after SIGTERM
        and Paused when the kill switch is on. (A handler stuck in one long call is SIGKILLed.)"""
        if self.stop_requested():
            raise Stopped()
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
        # Only set a flag: raising here could land between BEGIN and try in db.tx and leave a
        # transaction open. Handlers notice the flag in ctx.guard(); SIGKILL covers the rest.
        self._stop = True

    def run(self, max_seconds: float | None = None, types: list[str] | None = None) -> RunSummary:
        summary = RunSummary()
        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGTERM, self._on_sigterm)  # before start_run: no unguarded window
        control.reap_stale_runs(self.conn, self.clock)
        if control.is_paused(self.conn):
            log.info("paused; queue run skipped")
            summary.status = "paused"
            return summary
        run_id = control.start_run(self.conn, self.clock, "run queue")
        owner = f"{socket.gethostname()}:{os.getpid()}:{run_id}"
        side_effects = control.SideEffects(self.conn, self.clock, run_id=run_id)
        ctx = HandlerContext(self.conn, self.clock, self.cfg, self.notifier, side_effects, self.services,
                             stop_requested=lambda: self._stop)
        deadline = time.monotonic() + (max_seconds or self.cfg.settings.runner.max_seconds)
        final = "ok"
        try:
            side_effects.orphaned_to_unknown()
            summary.unknown_side_effects = self._notify_unknown(side_effects)
            for task in self.queue.reclaim_expired().dead:
                summary.dead += 1
                self._alert_dead(task.id, task.type, task.attempts, task.last_error or "")
            while time.monotonic() < deadline:
                if self._stop or control.is_paused(self.conn):
                    final = "stopped"
                    break
                # Only types this process can run: worker tasks (apply.*) stay for the laptop.
                task = self.queue.lease(owner, types or list(self.handlers))
                if task is None:
                    break
                self._run_one(task, ctx, summary)
            if self._stop:
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

    def _notify_unknown(self, side_effects: control.SideEffects) -> list[str]:
        """Tell the owner about interrupted actions. Marked notified only once delivered, so a
        Telegram outage means the next run tries again; /status always lists them too."""
        keys = side_effects.unknown(only_unnotified=True)
        if keys and self.notifier.send(
            "Прерванные внешние действия, исход неизвестен (повторять не буду, нужно твоё решение):\n"
            + "\n".join(f"• {k}" for k in keys)
            + "\nРешить: jobpipe effects resolve KEY done|failed|reset"
        ):
            side_effects.mark_notified(keys)
        return keys

    def _alert_dead(self, task_id: int, task_type: str, attempts: int, error: str) -> None:
        log.error("task dead-lettered", extra={"data": {"task": task_id, "type": task_type, "error": error}})
        self.notifier.send(
            f"Задача #{task_id} ({task_type}) ушла в dead-letter, попыток: {attempts}.\n"
            f"Последняя ошибка: {error[:500]}"
        )

    def _run_one(self, task: Task, ctx: HandlerContext, summary: RunSummary) -> None:
        handler = self.handlers.get(task.type)
        if handler is None:
            self._failed(task, f"no handler for task type {task.type!r}", summary, permanent=True)
            return
        try:
            handler(task, ctx)
        except Stopped:
            self.queue.release(task.id)
            self._stop = True
            return
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
            self._alert_dead(task.id, task.type, outcome.attempts, error)
        else:
            summary.retried += 1
            log.warning("task failed, will retry",
                        extra={"data": {"task": task.id, "type": task.type, "retry_in_s": outcome.retry_in_s}})
