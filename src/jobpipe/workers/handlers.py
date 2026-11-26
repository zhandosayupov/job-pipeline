"""Task handlers. S0 has self-test handlers and the LLM ping; real work arrives in S1+."""
from __future__ import annotations

import time

from pydantic import BaseModel

from ..services.events import record_event
from ..services.queue import Task
from .runner import Handler, HandlerContext


def selftest_ok(task: Task, ctx: HandlerContext) -> None:
    record_event(ctx.conn, ctx.clock, entity="task", entity_id=str(task.id), kind="note",
                 data={"selftest": "ok"})


def selftest_fail(task: Task, ctx: HandlerContext) -> None:
    raise RuntimeError(f"selftest failure on attempt {task.attempts + 1}")


def selftest_sleep(task: Task, ctx: HandlerContext) -> None:
    """Sleeps in short steps; used to demonstrate the kill switch."""
    seconds = float(task.payload.get("seconds", 60))
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        time.sleep(0.2)


class PingOut(BaseModel):
    ok: bool
    echo: str


def llm_ping(task: Task, ctx: HandlerContext) -> None:
    router = ctx.services["router"]()
    word = str(task.payload.get("word", "pong"))
    result = router.complete("ping", {"word": word}, schema=PingOut)
    record_event(ctx.conn, ctx.clock, entity="task", entity_id=str(task.id), kind="note",
                 data={"provider": result.provider, "echo": result.data.echo, "ms": result.latency_ms})


HANDLERS: dict[str, Handler] = {
    "selftest.ok": selftest_ok,
    "selftest.fail": selftest_fail,
    "selftest.sleep": selftest_sleep,
    "llm.ping": llm_ping,
}
