import asyncio
from types import SimpleNamespace

from jobpipe.adapters.storage.db import open_db
from jobpipe.bot.app import BotService, OwnerOnly
from jobpipe.services import control
from jobpipe.services.queue import Queue


def test_owner_only_middleware():
    mw = OwnerOnly(owner_id=1)
    seen = []

    async def handler(event, data):
        seen.append(data["event_from_user"].id)
        return "handled"

    async def go():
        a = await mw(handler, object(), {"event_from_user": SimpleNamespace(id=1)})
        b = await mw(handler, object(), {"event_from_user": SimpleNamespace(id=2)})
        c = await mw(handler, object(), {})
        return a, b, c

    assert asyncio.run(go()) == ("handled", None, None)
    assert seen == [1]


def test_service_pause_resume_status_health(app_cfg, clock):
    open_db(app_cfg.paths.db_path, clock).close()
    svc = BotService(app_cfg, clock)
    assert "Паузы и не было" in svc.resume()
    out = svc.pause()
    assert "Пауза включена" in out and "Активных задач не было" in out
    assert "ВКЛЮЧЕНА" in svc.status_text()
    health = svc.health()
    assert health["ok"] and health["paused"]
    assert svc.resume() == "Пауза снята."
    conn = open_db(app_cfg.paths.db_path, clock)
    assert not control.is_paused(conn)
    Queue(conn, clock, app_cfg.settings.queue).enqueue("selftest.ok")
    assert "pending 1" in svc.status_text()
