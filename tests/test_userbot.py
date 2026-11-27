"""S5 Telegram userbot without Telethon: message filtering, and the draft loop against the real
WorkerAPI over HTTP (aiohttp test server). Drafts are saved, never sent."""
import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
from aiohttp.test_utils import TestClient, TestServer

from jobpipe.adapters.notifier.telegram import RecordingNotifier
from jobpipe.adapters.storage.db import open_db
from jobpipe.api.worker import WorkerAPI
from jobpipe.bot.app import BotService, build_web_app
from jobpipe.config.settings import Secrets
from jobpipe.services import control
from jobpipe.services import inbox as ib
from jobpipe.services import kb as kbmod
from jobpipe.services.queue import Queue
from jobpipe.worker.userbot import Userbot, UserbotCfg, to_inbound
from jobpipe.workers.handlers import HANDLERS
from jobpipe.workers.runner import Runner

from .conftest import REPO

KB = kbmod.load(REPO / "config.example" / "kb")


def tg_msg(text="Hello, a C++ role?", private=True, chat=7, mid=1):
    return SimpleNamespace(is_private=private, message=text, chat_id=chat, id=mid,
                           date=datetime(2026, 11, 26, 9, 0, tzinfo=UTC))


def test_to_inbound_reads_private_human_text_only():
    person = SimpleNamespace(bot=False, id=7, first_name="Olga", last_name="K", username="olga_hr")
    item = to_inbound(tg_msg(), person, out=False)
    assert item == {"chat_id": 7, "msg_id": 1, "out": False, "text": "Hello, a C++ role?", "from_id": 7,
                    "from_name": "Olga K", "username": "olga_hr", "ts": "2026-11-26T09:00:00Z"}
    assert to_inbound(tg_msg(private=False), person, out=False) is None          # groups and channels
    assert to_inbound(tg_msg(), SimpleNamespace(bot=True), out=False) is None   # bots
    assert to_inbound(tg_msg(text="  "), person, out=False) is None             # stickers, photos


class FakeBot(Userbot):
    def __init__(self, cfg, http):
        super().__init__(cfg, client=None, http=http)
        self.saved = []

    async def save_draft(self, chat_id, text):
        self.saved.append((chat_id, text))


def test_userbot_round_over_http(app_cfg, clock):
    app_cfg.secrets = Secrets({"WORKER_TOKEN": "tok"})
    conn = open_db(app_cfg.paths.db_path, clock)
    notifier = RecordingNotifier()
    api = WorkerAPI(app_cfg, clock, notifier=notifier, kb_loader=lambda: KB)
    cfg = UserbotCfg(core_url="http://x", token="tok", api_id=1, api_hash="h", session=app_cfg.paths.home / "s")

    async def go():
        async with TestClient(TestServer(build_web_app(BotService(app_cfg, clock), api))) as c:
            base = str(c.make_url("/")).rstrip("/")
            async with httpx.AsyncClient(base_url=base, headers={"Authorization": "Bearer tok"}) as http:
                bot = FakeBot(cfg, http)
                person = SimpleNamespace(bot=False, id=7, first_name="Olga", last_name=None, username=None)
                event = SimpleNamespace(message=tg_msg("We have a C++ role. What is your notice period?"), out=False)

                async def sender():
                    return person
                event.get_sender = sender
                await bot.on_message(event)
                stored = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
                # the VM's queue runner triages it and drafts a reply
                Runner(conn, clock, app_cfg, HANDLERS, notifier,
                       services={"kb": lambda: KB, "router": lambda: None, "mailbox": None}).run(max_seconds=5)
                conv = conn.execute("SELECT rowid, id FROM conversations").fetchone()
                ib.message_decision(conn, clock, Queue(conn, clock, app_cfg.settings.queue), "ok", conv["rowid"])
                n = await bot.drafts_once()
                again = await bot.drafts_once()
                unauthorized = await httpx.AsyncClient(base_url=base).post("/worker/inbound", json={})
                return stored, n, again, bot.saved, unauthorized.status_code

    stored, n, again, saved, unauth = asyncio.run(go())
    assert stored == 1 and n == 1 and again == 0
    assert saved[0][0] == 7 and "Two weeks after signing the offer." in saved[0][1]
    assert unauth == 401
    key = conn.execute("SELECT key FROM side_effects WHERE kind = 'tg_draft'").fetchone()[0]
    assert control.SideEffects(conn, clock).status(key) == "done"
