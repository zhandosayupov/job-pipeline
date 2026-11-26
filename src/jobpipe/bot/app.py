"""The resident process (ADR 0001, 0013): Telegram bot for the owner + /healthz.

Each command opens its own short-lived SQLite connection in a worker thread, so the event loop
never blocks on the database and no connection is shared across threads.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from contextlib import closing
from typing import Any

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import BotCommand, Message, TelegramObject
from aiohttp import web

from .. import __version__
from ..adapters.storage.db import available_migrations, connect, current_version
from ..clock import Clock, SystemClock
from ..config.settings import AppConfig
from ..services import control
from ..services.queue import Queue
from . import status as status_mod

log = logging.getLogger(__name__)

COMMANDS = [
    BotCommand(command="status", description="Состояние системы"),
    BotCommand(command="pause", description="Стоп всех внешних действий"),
    BotCommand(command="resume", description="Снять паузу"),
    BotCommand(command="help", description="Список команд"),
]

HELP = (
    "Команды:\n"
    "/status — очередь, запуски, LLM, пауза\n"
    "/pause — остановить все внешние действия и текущие задачи (≤10 с)\n"
    "/resume — снять паузу\n"
)


class OwnerOnly(BaseMiddleware):
    """Drop every update that does not come from the owner's Telegram user id."""

    def __init__(self, owner_id: int):
        self.owner_id = owner_id

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None or user.id != self.owner_id:
            log.warning("update from non-owner ignored", extra={"data": {"user_id": getattr(user, "id", None)}})
            return None
        return await handler(event, data)


class BotService:
    def __init__(self, cfg: AppConfig, clock: Clock | None = None):
        self.cfg = cfg
        self.clock = clock or SystemClock()

    def _conn(self):
        return closing(connect(self.cfg.paths.db_path))

    # --- sync work, run in threads -----------------------------------------------------

    def status_text(self) -> str:
        with self._conn() as conn:
            q = Queue(conn, self.clock, self.cfg.settings.queue)
            snap = status_mod.gather(conn, self.clock, q, self.cfg.settings.timezone, str(self.cfg.paths.db_path))
            return status_mod.render(snap)

    def pause(self) -> str:
        with self._conn() as conn:
            control.set_flag(conn, self.clock, control.PAUSED, "1", actor="owner")
            report = control.stop_running(conn, self.clock, grace_s=self.cfg.settings.runner.kill_grace_s)
        parts = [f"Пауза включена. Остановка заняла {report.elapsed_s} с."]
        if report.terminated:
            parts.append(f"Остановлено процессов: {len(report.terminated)}"
                         + (f" (из них принудительно: {len(report.killed)})" if report.killed else ""))
        else:
            parts.append("Активных задач не было.")
        parts.append("Новые запуски будут сразу завершаться, пока не скажешь /resume.")
        return "\n".join(parts)

    def resume(self) -> str:
        with self._conn() as conn:
            was = control.is_paused(conn)
            control.set_flag(conn, self.clock, control.PAUSED, "0", actor="owner")
        return "Пауза снята." if was else "Паузы и не было."

    def health(self) -> dict[str, Any]:
        with self._conn() as conn:
            version = current_version(conn)
            latest = len(available_migrations())
            return {
                "ok": version == latest,
                "version": __version__,
                "schema": version,
                "schema_latest": latest,
                "paused": control.is_paused(conn),
            }


def build_dispatcher(service: BotService, owner_id: int) -> Dispatcher:
    dp = Dispatcher()
    dp.message.outer_middleware(OwnerOnly(owner_id))
    dp.callback_query.outer_middleware(OwnerOnly(owner_id))

    @dp.message(Command("start", "help"))
    async def _help(message: Message) -> None:
        await message.answer(HELP)

    @dp.message(Command("status"))
    async def _status(message: Message) -> None:
        await message.answer(await asyncio.to_thread(service.status_text))

    @dp.message(Command("pause"))
    async def _pause(message: Message) -> None:
        await message.answer(await asyncio.to_thread(service.pause))

    @dp.message(Command("resume"))
    async def _resume(message: Message) -> None:
        await message.answer(await asyncio.to_thread(service.resume))

    return dp


def build_web_app(service: BotService) -> web.Application:
    async def healthz(request: web.Request) -> web.Response:
        try:
            body = await asyncio.to_thread(service.health)
        except Exception as e:  # the endpoint must answer even when the DB is broken
            body = {"ok": False, "error": type(e).__name__}
        return web.json_response(body, status=200 if body.get("ok") else 503, dumps=json.dumps)

    app = web.Application()
    app.router.add_get("/healthz", healthz)
    return app


async def run_bot(cfg: AppConfig) -> None:
    token = cfg.secrets.get(cfg.settings.telegram.token_env)
    owner_id = cfg.settings.telegram.owner_id
    if not token or not owner_id:
        raise SystemExit(
            f"Telegram is not configured: set {cfg.settings.telegram.token_env} in secrets.env "
            "and telegram.owner_id in settings.yaml"
        )
    service = BotService(cfg)
    bot = Bot(token)
    dp = build_dispatcher(service, owner_id)

    runner = web.AppRunner(build_web_app(service))
    await runner.setup()
    site = web.TCPSite(runner, cfg.settings.http.host, cfg.settings.http.port)
    await site.start()
    log.info("healthz listening", extra={"data": {"host": cfg.settings.http.host, "port": cfg.settings.http.port}})

    try:
        await bot.set_my_commands(COMMANDS)
        await bot.send_message(owner_id, f"jobpipe-bot {__version__} запущен.", disable_notification=True)
    except Exception:
        log.exception("startup message failed")
    try:
        await dp.start_polling(bot, handle_signals=True)
    finally:
        await runner.cleanup()
        await bot.session.close()
