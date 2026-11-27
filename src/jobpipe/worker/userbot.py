"""`jobpipe userbot`: recruiter messages on the owner's secondary Telegram account (laptop, S5).

* Private chats only (no groups, channels or bots). Every incoming message and the owner's own
  replies are posted to the VM (`/worker/inbound`), where they are triaged like e-mail.
* An approved reply comes back as a `tg.draft` task: it is saved as a **draft** in that chat
  (messages.saveDraft), never sent. The owner opens Telegram, checks it and presses send.
* Login once, interactively, by the owner: `jobpipe userbot --login` (phone number and the code
  Telegram sends; 2FA password if set). API id/hash come from my.telegram.org and live in worker.env.
  The session file is a full credential for the account: it stays in the worker directory.

Telethon is an optional dependency (`uv sync --group userbot`); the VM never installs it.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path

import httpx

log = logging.getLogger("jobpipe.userbot")


@dataclass
class UserbotCfg:
    core_url: str
    token: str
    api_id: int
    api_hash: str
    session: Path
    worker_id: str = "userbot"
    poll_s: float = 30


def to_inbound(msg, sender, *, out: bool) -> dict | None:
    """A Telethon message -> the /worker/inbound shape; None for chats we do not read."""
    if not getattr(msg, "is_private", False):
        return None
    if sender is not None and getattr(sender, "bot", False):
        return None
    text = getattr(msg, "message", None) or getattr(msg, "text", None) or ""
    if not text.strip():
        return None
    ts = getattr(msg, "date", None)
    name = " ".join(x for x in (getattr(sender, "first_name", None), getattr(sender, "last_name", None)) if x)
    return {
        "chat_id": msg.chat_id, "msg_id": msg.id, "out": out, "text": text[:6000],
        "from_id": getattr(sender, "id", None), "from_name": name, "username": getattr(sender, "username", None),
        "ts": ts.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z") if ts else None,
    }


class Userbot:
    def __init__(self, cfg: UserbotCfg, client=None, http: httpx.AsyncClient | None = None):
        self.cfg = cfg
        self.client = client
        self.http = http or httpx.AsyncClient(base_url=cfg.core_url.rstrip("/"), timeout=40,
                                              headers={"Authorization": f"Bearer {cfg.token}"})

    async def post(self, items: list[dict]) -> None:
        if items:
            r = await self.http.post("/worker/inbound", json={"messages": items})
            r.raise_for_status()

    async def on_message(self, event) -> None:
        try:
            sender = await event.get_sender()
            item = to_inbound(event.message, sender, out=bool(event.out))
            if item:
                await self.post([item])
        except Exception as e:  # the listener must survive one bad message or a VM restart
            log.warning("inbound failed: %s", type(e).__name__)

    async def drafts_once(self) -> int:
        """Lease tg.draft tasks and save each as a draft in its chat."""
        hb = (await self.http.post("/worker/heartbeat", json={"worker": self.cfg.worker_id,
                                                              "info": {"kind": "userbot"}})).json()
        if hb.get("paused"):
            return 0
        n = 0
        while True:
            r = await self.http.get("/worker/lease", params={"worker": self.cfg.worker_id, "wait": 0,
                                                             "types": "tg.draft"})
            r.raise_for_status()
            task = r.json().get("task")
            if not task:
                return n
            result = {"key": task["key"], "ok": False}
            try:
                await self.save_draft(int(task["chat_id"]), task["text"])
                result["ok"] = True
            except Exception as e:
                result["error"] = type(e).__name__
            files = [("json", (None, json.dumps({"task_id": task["task_id"], "kind": "tg_draft",
                                                                "app_id": task["app_id"], "result": result}),
                               "application/json"))]
            (await self.http.post("/worker/result", files=files)).raise_for_status()
            n += 1

    async def save_draft(self, chat_id: int, text: str) -> None:  # pragma: no cover - Telethon call
        from telethon.tl.functions.messages import SaveDraftRequest

        peer = await self.client.get_input_entity(chat_id)
        await self.client(SaveDraftRequest(peer=peer, message=text))

    async def run(self) -> None:  # pragma: no cover - needs a logged-in Telegram session
        from telethon import events

        self.client.add_event_handler(self.on_message, events.NewMessage(func=lambda e: e.is_private))
        while True:
            try:
                await self.drafts_once()
            except httpx.HTTPError as e:
                log.warning("core unreachable: %s", type(e).__name__)
            await asyncio.sleep(self.cfg.poll_s)


def make_client(cfg: UserbotCfg):  # pragma: no cover - thin Telethon wrapper
    from telethon import TelegramClient

    cfg.session.parent.mkdir(parents=True, exist_ok=True)
    return TelegramClient(str(cfg.session), cfg.api_id, cfg.api_hash, device_model="jobpipe", app_version="0.1")


async def login(cfg: UserbotCfg) -> None:  # pragma: no cover - interactive
    client = make_client(cfg)
    await client.start()   # asks for the phone, the code and the 2FA password on the console
    me = await client.get_me()
    print(f"logged in as id {me.id}; session saved to {cfg.session}")
    await client.disconnect()


async def serve(cfg: UserbotCfg) -> None:  # pragma: no cover - needs a logged-in session
    client = make_client(cfg)
    await client.connect()
    if not await client.is_user_authorized():
        raise SystemExit("not logged in: run `jobpipe userbot --login` once")
    bot = Userbot(cfg, client)
    async with client:
        await asyncio.gather(client.run_until_disconnected(), bot.run())
