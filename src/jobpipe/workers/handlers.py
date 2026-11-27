"""Task handlers: S0 self-tests and the LLM ping; S5 inbox drafts, redrafts and message processing."""
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
        ctx.guard()  # cooperative stop on /pause or SIGTERM
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


def inbox_draft(task: Task, ctx: HandlerContext) -> None:
    """Put an approved reply into Gmail Drafts (IMAP APPEND). At most once: the key is reserved before
    the APPEND; a failure after that is `unknown` for the owner, never retried blindly."""
    from ..services import inbox

    conv_id, key = task.payload["conv_id"], task.payload["key"]
    kb = ctx.services["kb"]()
    raw_from = ctx.cfg.settings.inbox.gmail_user or (kb.profile.email if kb else None)
    if not raw_from:
        raise RuntimeError("no Gmail address: set inbox.gmail_user in settings.yaml")
    raw = inbox.draft_bytes(ctx.conn, ctx.cfg, kb, conv_id, raw_from)
    with ctx.services["mailbox"]() as mb:      # login errors happen here, before the key: safe to retry
        ctx.guard()
        if not ctx.side_effects.plan(key, "gmail_draft", {"conv_id": conv_id}):
            return                              # already done (or unknown): never twice
        try:
            mb.append_draft(raw)
        except Exception as e:
            ctx.side_effects.mark(key, "unknown")
            ctx.notifier.send(f"❓ Черновик в Gmail мог не создаться ({type(e).__name__}). Проверь «Черновики»; "
                              f"если его нет: jobpipe effects resolve {key} reset и одобри заново.")
            return
    ctx.side_effects.mark(key, "done")
    record_event(ctx.conn, ctx.clock, entity="conversation", entity_id=conv_id, kind="draft_created",
                 data={"key": key})
    ctx.notifier.send("📥 Черновик ответа лежит в Gmail → «Черновики». Проверь, заполни [[ВПИШИ]] и отправь сам.",
                      silent=True)


def inbox_reply(task: Task, ctx: HandlerContext) -> None:
    """Owner pressed «Переделать»: a new draft and card for the conversation."""
    from ..services import inbox
    from ..services.queue import Queue

    kb = ctx.services["kb"]()
    if kb is None:
        raise RuntimeError("knowledge base not available")
    inbox.redraft(ctx.conn, ctx.clock, ctx.cfg, kb, ctx.services["router"](), ctx.notifier,
                  Queue(ctx.conn, ctx.clock, ctx.cfg.settings.queue), task.payload["conv_id"])


def inbox_process(task: Task, ctx: HandlerContext) -> None:
    """Messages stored by the Telegram userbot API: triage them within a minute instead of 15."""
    from ..services import inbox
    from ..services.queue import Queue

    ctx.guard()
    inbox.process(ctx.conn, ctx.clock, ctx.cfg, ctx.services["kb"](), ctx.services["router"](), ctx.notifier,
                  Queue(ctx.conn, ctx.clock, ctx.cfg.settings.queue), collector=None, limit=50)


HANDLERS: dict[str, Handler] = {
    "selftest.ok": selftest_ok,
    "selftest.fail": selftest_fail,
    "selftest.sleep": selftest_sleep,
    "llm.ping": llm_ping,
    "inbox.draft": inbox_draft,
    "inbox.reply": inbox_reply,
    "inbox.process": inbox_process,
}
