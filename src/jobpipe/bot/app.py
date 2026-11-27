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

from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
)
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
    BotCommand(command="shortlist", description="Лучшие вакансии"),
    BotCommand(command="funnel", description="Воронка"),
    BotCommand(command="sources", description="Источники"),
    BotCommand(command="rate", description="Оценить разметку"),
    BotCommand(command="pause", description="Стоп всех внешних действий"),
    BotCommand(command="resume", description="Снять паузу"),
    BotCommand(command="help", description="Список команд"),
]

HELP = (
    "Команды:\n"
    "/status — очередь, запуски, LLM, источники, пауза\n"
    "/shortlist — лучшие вакансии после разметки\n"
    "/funnel — сколько вакансий и заявок на каждом этапе\n"
    "/sources — последний запуск каждого источника\n"
    "/rate — оценить случайную разметку (👍/👎), считает % согласия\n"
    "/protect <компания> · /unprotect <компания> — protected pool\n"
    "/dryrun <платформа> on|off — DRY_RUN для подачи\n"
    "/reload — перечитать конфиг\n"
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

    # --- S2: protected pool, shortlist, funnel, cards ----------------------------------

    def reload(self) -> str:
        from ..config.settings import load_config

        self.cfg = load_config(self.cfg.paths.home)
        return "Конфиг перечитан. (Задачи по таймерам и так читают его при каждом запуске.)"

    def protect(self, name: str) -> str:
        from ..services import protected as prot

        if not name.strip():
            return "Использование: /protect <компания>"
        with self._conn() as conn:
            added = prot.protect(self.cfg.paths.config_dir / "protected.yaml", conn, self.clock, name)
        return f"🛡 {name}: добавлена в protected." if added else f"🛡 {name}: уже в protected."

    def unprotect(self, name: str) -> str:
        from ..services import protected as prot

        if not name.strip():
            return "Использование: /unprotect <компания>"
        with self._conn() as conn:
            removed = prot.unprotect(self.cfg.paths.config_dir / "protected.yaml", conn, self.clock, name)
        return f"{name}: убрана из protected." if removed else f"{name}: в protected её нет."

    def dryrun(self, platform: str, value: str) -> str:
        if not platform or value not in ("on", "off"):
            return "Использование: /dryrun <платформа> on|off   (например /dryrun hh off)"
        with self._conn() as conn:
            control.set_flag(conn, self.clock, f"dry_run.{platform}", "1" if value == "on" else "0", actor="owner")
        return (f"DRY_RUN для {platform}: {'включён — формы только заполняются' if value == 'on' else 'выключен'}.")

    def sources_text(self) -> str:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT s.source, s.status, s.finished_at, s.fetched, s.new, s.kept, s.error FROM source_runs s "
                "JOIN (SELECT source, MAX(id) id FROM source_runs GROUP BY source) m ON m.id = s.id ORDER BY s.source"
            ).fetchall()
        if not rows:
            return "Источники ещё не запускались."
        lines = ["Источники (последний запуск):"]
        for r in rows:
            err = f" — {json.loads(r['error'])[0][:80]}" if r["error"] else ""
            lines.append(f"{'✅' if r['status'] == 'ok' else '⚠️'} {r['source']}: {r['status']}, "
                         f"получено {r['fetched']}, новых {r['new']}, прошло {r['kept']}{err}")
        return "\n".join(lines)

    def shortlist_text(self, limit: int = 15) -> str:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT company, title, url, protected, json_extract(data_json, '$.label.score') AS score "
                "FROM jobs WHERE state = 'shortlisted' ORDER BY protected DESC, score DESC LIMIT ?", (limit,)
            ).fetchall()
        if not rows:
            return "Шортлист пуст."
        return "Шортлист:\n" + "\n".join(
            f"{'🛡 ' if r['protected'] else ''}{r['score'] or '?'} · {r['company']} — {r['title']}\n  {r['url']}"
            for r in rows)

    def funnel_text(self) -> str:
        with self._conn() as conn:
            jobs = dict(conn.execute("SELECT state, COUNT(*) FROM jobs GROUP BY state").fetchall())
            apps = dict(conn.execute("SELECT state, COUNT(*) FROM applications GROUP BY state").fetchall())
            from ..services.cards import agreement

            up, total = agreement(conn)
        order = ["discovered", "prefiltered", "labeled", "shortlisted", "skipped", "dropped", "closed"]
        lines = ["Вакансии: " + ", ".join(f"{s} {jobs[s]}" for s in order if jobs.get(s))]
        lines.append("Заявки: " + (", ".join(f"{k} {v}" for k, v in sorted(apps.items())) or "нет"))
        if total:
            lines.append(f"Оценка разметки: согласен {up}/{total} ({round(100 * up / total)}%)")
        return "\n".join(lines)

    def job_action(self, action: str, rowid: int) -> str:
        from ..services.cards import handle_job_action

        with self._conn() as conn:
            return handle_job_action(conn, self.clock, self.cfg, action, rowid)

    def app_action(self, action: str, rowid: int) -> str:
        from ..services.cards import handle_app_action

        with self._conn() as conn:
            return handle_app_action(conn, self.clock, action, rowid)

    def submit_decision(self, action: str, rowid: int) -> str:
        from ..services.apply import owner_decision

        with self._conn() as conn:
            return owner_decision(conn, self.clock, Queue(conn, self.clock, self.cfg.settings.queue), action, rowid)

    def rate_next(self):
        from ..services.cards import next_rating_card

        with self._conn() as conn:
            return next_rating_card(conn)

    def rate(self, vote: str, rowid: int) -> str:
        from ..services.cards import record_rating

        with self._conn() as conn:
            return record_rating(conn, self.clock, rowid, vote)

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

    @dp.message(Command("protect"))
    async def _protect(message: Message, command: CommandObject) -> None:
        await message.answer(await asyncio.to_thread(service.protect, command.args or ""))

    @dp.message(Command("unprotect"))
    async def _unprotect(message: Message, command: CommandObject) -> None:
        await message.answer(await asyncio.to_thread(service.unprotect, command.args or ""))

    @dp.message(Command("dryrun"))
    async def _dryrun(message: Message, command: CommandObject) -> None:
        parts = (command.args or "").split()
        await message.answer(await asyncio.to_thread(service.dryrun, parts[0] if parts else "",
                                                     parts[1] if len(parts) > 1 else ""))

    @dp.message(Command("sources"))
    async def _sources(message: Message) -> None:
        await message.answer(await asyncio.to_thread(service.sources_text))

    @dp.message(Command("shortlist"))
    async def _shortlist(message: Message) -> None:
        await message.answer(await asyncio.to_thread(service.shortlist_text), disable_web_page_preview=True)

    @dp.message(Command("funnel"))
    async def _funnel(message: Message) -> None:
        await message.answer(await asyncio.to_thread(service.funnel_text))

    @dp.message(Command("reload"))
    async def _reload(message: Message) -> None:
        await message.answer(await asyncio.to_thread(service.reload))

    @dp.message(Command("rate"))
    async def _rate(message: Message) -> None:
        card = await asyncio.to_thread(service.rate_next)
        if card is None:
            await message.answer("Нечего оценивать: размеченных вакансий нет или все уже оценены.")
            return
        await message.answer(card[0], reply_markup=_keyboard(card[1]), disable_web_page_preview=True)

    @dp.callback_query(F.data.startswith("j:"))
    async def _job_button(cb: CallbackQuery) -> None:
        _, action, rowid = cb.data.split(":", 2)
        result = await asyncio.to_thread(service.job_action, action, int(rowid))
        await cb.answer(result[:190])
        if cb.message is not None:
            await cb.message.edit_reply_markup(reply_markup=None)
            await cb.message.answer(result, disable_web_page_preview=True)

    @dp.callback_query(F.data.startswith("a:"))
    async def _app_button(cb: CallbackQuery) -> None:
        _, action, rowid = cb.data.split(":", 2)
        result = await asyncio.to_thread(service.app_action, action, int(rowid))
        await cb.answer(result[:190])
        if cb.message is not None:
            await cb.message.edit_reply_markup(reply_markup=None)
            await cb.message.answer(result)

    @dp.callback_query(F.data.startswith("s:"))
    async def _submit_button(cb: CallbackQuery) -> None:
        _, action, rowid = cb.data.split(":", 2)
        result = await asyncio.to_thread(service.submit_decision, action, int(rowid))
        await cb.answer(result[:190])
        if cb.message is not None:
            await cb.message.edit_reply_markup(reply_markup=None)
            await cb.message.answer(result)

    @dp.callback_query(F.data.startswith("r:"))
    async def _rate_button(cb: CallbackQuery) -> None:
        _, vote, rowid = cb.data.split(":", 2)
        result = await asyncio.to_thread(service.rate, vote, int(rowid))
        await cb.answer(result[:190])
        if cb.message is not None:
            await cb.message.edit_reply_markup(reply_markup=None)
            nxt = await asyncio.to_thread(service.rate_next)
            if nxt is None:
                await cb.message.answer(result + "\nВсё оценено.")
            else:
                await cb.message.answer(nxt[0], reply_markup=_keyboard(nxt[1]), disable_web_page_preview=True)

    return dp


def _keyboard(buttons) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in row]
                                                 for row in buttons])


def build_web_app(service: BotService, worker_api=None) -> web.Application:
    async def healthz(request: web.Request) -> web.Response:
        try:
            body = await asyncio.to_thread(service.health)
        except Exception as e:  # the endpoint must answer even when the DB is broken
            body = {"ok": False, "error": type(e).__name__}
        return web.json_response(body, status=200 if body.get("ok") else 503, dumps=json.dumps)

    app = web.Application(client_max_size=20 * 1024 * 1024)  # screenshots
    app.router.add_get("/healthz", healthz)
    if worker_api is not None and worker_api._token():
        app.add_routes(worker_api.routes())
    return app


def _kb_loader(cfg: AppConfig):
    def load():
        from ..services import kb as kbmod

        try:
            return kbmod.load(cfg.paths.home / "kb")
        except Exception:
            log.exception("knowledge base could not be loaded")
            return None
    return load


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

    from ..adapters.notifier.telegram import TelegramNotifier
    from ..api.worker import WorkerAPI

    worker_api = WorkerAPI(cfg, notifier=TelegramNotifier(token, owner_id), kb_loader=_kb_loader(cfg))
    runner = web.AppRunner(build_web_app(service, worker_api))
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
