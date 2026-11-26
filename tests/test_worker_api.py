"""The worker API over real HTTP (aiohttp test server): auth, heartbeat, lease, resolve, artifact, result."""
import asyncio
import json

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from jobpipe.adapters.notifier.telegram import RecordingNotifier
from jobpipe.adapters.storage.db import open_db
from jobpipe.api.worker import WorkerAPI
from jobpipe.bot.app import BotService, build_web_app
from jobpipe.config.settings import Secrets
from jobpipe.services import apply as ap
from jobpipe.services import control
from jobpipe.services import kb as kbmod
from jobpipe.services.events import create_entity, transition
from jobpipe.services.queue import Queue

from .conftest import REPO

KB = kbmod.load(REPO / "config.example" / "kb")


def setup(app_cfg, clock, tmp_path):
    app_cfg.secrets = Secrets({"WORKER_TOKEN": "s3cret-token"})
    conn = open_db(app_cfg.paths.db_path, clock)
    create_entity(conn, clock, "job", "gh:a:1", columns={"source": "greenhouse", "kind": "ats", "company": "Acme",
                                                         "title": "Eng", "url": "https://boards.greenhouse.io/a/1",
                                                         "country": "KZ"})
    cv = tmp_path / "cv.tex"
    cv.write_text("TEX", encoding="utf-8")
    create_entity(conn, clock, "application", "app:gh:a:1", columns={"job_id": "gh:a:1", "idempotency_key": "k1"},
                  data={"approved_cv": True, "artifacts": {"tex": str(cv)}})
    transition(conn, clock, "application", "app:gh:a:1", "tailoring")
    transition(conn, clock, "application", "app:gh:a:1", "ready")
    notifier = RecordingNotifier()
    ap.schedule(conn, clock, app_cfg, Queue(conn, clock, app_cfg.settings.queue), notifier)
    api = WorkerAPI(app_cfg, clock, notifier=notifier, kb_loader=lambda: KB)
    return conn, api, notifier


def run(coro):
    return asyncio.run(coro)


def test_routes_require_the_token(app_cfg, clock, tmp_path):
    _, api, _ = setup(app_cfg, clock, tmp_path)

    async def go():
        async with TestClient(TestServer(build_web_app(BotService(app_cfg, clock), api))) as c:
            r1 = await c.post("/worker/heartbeat", json={"worker": "w"})
            r2 = await c.post("/worker/heartbeat", json={"worker": "w"}, headers={"Authorization": "Bearer nope"})
            r3 = await c.get("/healthz")
            return r1.status, r2.status, r3.status

    assert run(go()) == (401, 401, 200)


def test_no_routes_without_a_token(app_cfg, clock, tmp_path):
    setup(app_cfg, clock, tmp_path)
    app_cfg.secrets = Secrets({})

    async def go():
        async with TestClient(TestServer(build_web_app(BotService(app_cfg, clock), WorkerAPI(app_cfg, clock)))) as c:
            return (await c.post("/worker/heartbeat", json={})).status

    assert run(go()) == 404


def test_full_http_round(app_cfg, clock, tmp_path):
    conn, api, notifier = setup(app_cfg, clock, tmp_path)
    auth = {"Authorization": "Bearer s3cret-token"}

    async def go():
        async with TestClient(TestServer(build_web_app(BotService(app_cfg, clock), api))) as c:
            hb = await (await c.post("/worker/heartbeat", json={"worker": "laptop", "info": {"ready": True}},
                                     headers=auth)).json()
            task = (await (await c.get("/worker/lease", params={"worker": "laptop", "wait": 0}, headers=auth)).json())
            task = task["task"]
            fields = [{"key": "#e", "label": "Email", "kind": "email", "required": True},
                      {"key": "#cv", "label": "Resume", "kind": "file", "required": True}]
            res = await (await c.post("/worker/resolve", json={"app_id": task["app_id"], "fields": fields},
                                      headers=auth)).json()
            art = await c.get(f"/worker/artifact/{task['app_id']}/tex", headers=auth)
            art_body = await art.text()
            form = aiohttp.FormData()
            form.add_field("json", json.dumps({"task_id": task["task_id"], "kind": "fill", "app_id": task["app_id"],
                                               "result": {"ok": True, "manifest_hash": res["hash"]}}))
            form.add_field("screenshot", b"\x89PNG", filename="s.png", content_type="image/png")
            out = await (await c.post("/worker/result", data=form, headers=auth)).json()
            empty = await (await c.get("/worker/lease", params={"wait": 0}, headers=auth)).json()
            return hb, task, res, art_body, out, empty

    hb, task, res, art_body, out, empty = run(go())
    assert hb == {"paused": False}
    assert task["type"] == "apply.fill" and task["platform"] == "greenhouse"
    assert {a["key"]: a["value"] for a in res["answers"]}["#e"] == "alex@example.com"
    assert art_body == "TEX"
    assert out == {"outcome": "dry_run"}                     # dry run is on by default
    assert empty == {"task": None}
    assert conn.execute("SELECT state FROM applications").fetchone()[0] == "awaiting_approval"
    assert notifier.files and notifier.files[-1][0].endswith(".png")
    assert conn.execute("SELECT id FROM workers").fetchone()[0] == "laptop"


def test_paused_lease_returns_nothing_and_captcha_is_alerted_once(app_cfg, clock, tmp_path):
    conn, api, notifier = setup(app_cfg, clock, tmp_path)
    control.set_flag(conn, clock, control.PAUSED, "1")
    assert api.lease_once("w") is None
    assert api.heartbeat("w", {"captcha": "https://x/1"})["paused"] is True
    api.heartbeat("w", {"captcha": "https://x/1"})
    assert sum("капчу" in m for m in notifier.messages) == 1


def test_web_app_accepts_big_screenshots(app_cfg, clock):
    app = build_web_app(BotService(app_cfg, clock), None)
    assert isinstance(app, web.Application) and app._client_max_size >= 10 * 1024 * 1024
