"""HTTP API for the laptop browser worker (ADR 0006), mounted in the resident bot process.

All routes need `Authorization: Bearer <WORKER_TOKEN>` (secrets.env). Reach it over Tailscale only:
set settings.yaml `http.host` to the VM's Tailscale address (never open the port publicly).

  POST /worker/heartbeat           {"worker": id, "info": {...}} -> {"paused": bool}
  GET  /worker/lease?worker=&wait= long-poll up to `wait` s (<= 25) -> {"task": {...}} or {"task": null}
  POST /worker/resolve             {"app_id", "fields": [FormField]} -> {"answers": [...], "unanswered": [...]}
  GET  /worker/artifact/{app}/{name}  CV (pdf|tex) or letter file of an application
  POST /worker/result              multipart: "json" {"task_id", "kind": fill|submit, "app_id", "result"},
                                   optional "screenshot" file
"""
from __future__ import annotations

import asyncio
import hmac
import json
import re
import time
from contextlib import closing
from pathlib import Path

from aiohttp import web

from ..adapters.storage.db import connect
from ..clock import Clock, SystemClock, iso
from ..config.settings import AppConfig
from ..services import apply as ap
from ..services import control
from ..services.forms import FormField
from ..services.queue import Queue

TASK_TYPES = [ap.FILL, ap.SUBMIT]


class WorkerAPI:
    def __init__(self, cfg: AppConfig, clock: Clock | None = None, notifier=None, kb_loader=None):
        self.cfg = cfg
        self.clock = clock or SystemClock()
        self.notifier = notifier
        self.kb_loader = kb_loader
        self._captcha_alerted: set[str] = set()

    def _conn(self):
        return closing(connect(self.cfg.paths.db_path))

    def _token(self) -> str | None:
        return self.cfg.secrets.get("WORKER_TOKEN")

    def authorized(self, request: web.Request) -> bool:
        token = self._token()
        given = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        return bool(token) and hmac.compare_digest(given, token)

    # --- sync work -------------------------------------------------------------------------

    def heartbeat(self, worker: str, info: dict) -> dict:
        captcha_url = info.get("captcha")
        if captcha_url and captcha_url not in self._captcha_alerted and self.notifier is not None:
            self._captcha_alerted.add(captcha_url)
            self.notifier.send(f"🧩 Воркер упёрся в капчу/проверку: {captcha_url}\n"
                               "Реши её в окне браузера на ноуте — задача продолжится сама (ждёт до 10 минут). "
                               "Обходить капчу jobpipe не будет.")
        with self._conn() as conn:
            conn.execute("INSERT INTO workers(id, last_seen, info_json) VALUES (?, ?, ?) ON CONFLICT(id) DO UPDATE "
                         "SET last_seen = excluded.last_seen, info_json = excluded.info_json",
                         (worker[:64], iso(self.clock.now()), json.dumps(info, ensure_ascii=False)[:4000]))
            return {"paused": control.is_paused(conn)}

    def lease_once(self, worker: str) -> dict | None:
        with self._conn() as conn:
            if control.is_paused(conn):
                return None
            q = Queue(conn, self.clock, self.cfg.settings.queue)
            q.reclaim_expired()
            t = q.lease(f"worker:{worker[:64]}", TASK_TYPES)
            if t is None:
                return None
            return {"task_id": t.id, "type": t.type, **t.payload}

    def resolve(self, app_id: str, fields: list[dict]) -> dict:
        kb = self.kb_loader() if self.kb_loader else None
        if kb is None:
            raise web.HTTPServiceUnavailable(text="knowledge base not available")
        with self._conn() as conn:
            res = ap.resolve_for(conn, self.cfg, kb, app_id, [FormField.model_validate(f) for f in fields])
        return {"answers": [a.model_dump() for a in res.answers],
                "unanswered": [f.model_dump() for f in res.unanswered], "hash": res.manifest_hash()}

    def artifact_path(self, app_id: str, name: str) -> Path | None:
        with self._conn() as conn:
            row = conn.execute("SELECT data_json FROM applications WHERE id = ?", (app_id,)).fetchone()
        if row is None:
            return None
        arts = (json.loads(row[0] or "{}")).get("artifacts", {})
        path = arts.get(name)
        return Path(path) if path and Path(path).exists() else None

    def result(self, payload: dict, screenshot: Path | None) -> dict:
        kind, app_id, task_id = payload["kind"], payload["app_id"], int(payload["task_id"])
        with self._conn() as conn:
            q = Queue(conn, self.clock, self.cfg.settings.queue)
            if kind == "fill":
                r = ap.FillResult.model_validate({**payload["result"], "screenshot": str(screenshot) if screenshot
                                                  else None})
                route = ap.on_fill_result(conn, self.clock, self.cfg, q, self.notifier, app_id, r)
                outcome = route.value if route else "failed"
            else:
                r = ap.SubmitResult.model_validate({**payload["result"], "screenshot": str(screenshot) if screenshot
                                                    else None})
                kb = self.kb_loader() if self.kb_loader else None
                outcome = ap.on_submit_result(conn, self.clock, self.cfg, kb, self.notifier, app_id, r)
            q.complete(task_id)
        return {"outcome": outcome}

    def screenshot_dir(self, app_id: str) -> Path:
        d = Path(self.cfg.paths.data_dir) / "artifacts" / re.sub(r"[^\w.-]+", "_", app_id)
        d.mkdir(parents=True, exist_ok=True)
        return d

    # --- aiohttp ---------------------------------------------------------------------------

    def routes(self) -> list[web.RouteDef]:
        def guard(handler):
            async def wrapped(request: web.Request):
                if not self.authorized(request):
                    raise web.HTTPUnauthorized()
                return await handler(request)
            return wrapped

        async def heartbeat(request):
            body = await request.json()
            return web.json_response(await asyncio.to_thread(self.heartbeat, str(body.get("worker", "?")),
                                                             body.get("info") or {}))

        async def lease(request):
            worker = request.query.get("worker", "?")
            wait = min(float(request.query.get("wait", 25)), 25.0)
            deadline = time.monotonic() + wait
            while True:
                task = await asyncio.to_thread(self.lease_once, worker)
                if task or time.monotonic() >= deadline:
                    return web.json_response({"task": task})
                await asyncio.sleep(1.0)

        async def resolve(request):
            body = await request.json()
            return web.json_response(await asyncio.to_thread(self.resolve, body["app_id"], body["fields"]))

        async def artifact(request):
            path = await asyncio.to_thread(self.artifact_path, request.match_info["app"], request.match_info["name"])
            if path is None:
                raise web.HTTPNotFound()
            return web.FileResponse(path)

        async def result(request):
            reader = await request.multipart()
            payload, shot = None, None
            while (part := await reader.next()) is not None:
                if part.name == "json":
                    payload = json.loads(await part.text())
                elif part.name == "screenshot" and payload is not None:
                    d = self.screenshot_dir(payload["app_id"])
                    shot = d / f"{payload['kind']}-{int(time.time())}.png"
                    shot.write_bytes(await part.read())
            if payload is None:
                raise web.HTTPBadRequest(text="json part required first")
            return web.json_response(await asyncio.to_thread(self.result, payload, shot))

        return [
            web.post("/worker/heartbeat", guard(heartbeat)),
            web.get("/worker/lease", guard(lease)),
            web.post("/worker/resolve", guard(resolve)),
            web.get("/worker/artifact/{app}/{name}", guard(artifact)),
            web.post("/worker/result", guard(result)),
        ]
