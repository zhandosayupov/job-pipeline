"""`jobpipe worker`: the laptop side (ADR 0006). Long-polls the VM for apply tasks and runs them in a
real browser, one at a time. It decides nothing: answers come from the VM, submit happens only for an
`apply.submit` task, and the pause flag is checked again right before the click.

Started by Windows Task Scheduler at logon (ops/worker/install-worker.ps1): hidden, restarted on
failure, BELOW_NORMAL priority; applies only on AC power after `min_idle_s` without input.
"""
from __future__ import annotations

import contextlib
import json
import logging
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from . import system
from .appliers import Applier, ApplierCfg, ApplyError
from .page import Page

log = logging.getLogger("jobpipe.worker")


@dataclass
class WorkerCfg:
    core_url: str                       # http://<vm tailscale ip>:8080
    token: str
    worker_id: str = "laptop"
    profiles_dir: Path = field(default_factory=lambda: Path.home() / ".jobpipe-worker" / "profiles")
    headless: bool = True
    min_idle_s: float = 300             # owner away from the keyboard this long before applying
    require_ac: bool = True
    captcha_wait_s: float = 600         # how long to wait for the owner to solve a captcha


class Core:
    """The VM API, as the worker uses it."""

    def __init__(self, cfg: WorkerCfg, client: httpx.Client | None = None):
        self.cfg = cfg
        self.client = client or httpx.Client(base_url=cfg.core_url.rstrip("/"), timeout=60,
                                             headers={"Authorization": f"Bearer {cfg.token}"})

    def heartbeat(self, info: dict) -> dict:
        return self.client.post("/worker/heartbeat", json={"worker": self.cfg.worker_id, "info": info}).json()

    def lease(self, wait: float = 25) -> dict | None:
        r = self.client.get("/worker/lease", params={"worker": self.cfg.worker_id, "wait": wait},
                            timeout=wait + 15)
        r.raise_for_status()
        return r.json().get("task")

    def resolve(self, app_id: str, fields: list) -> dict:
        r = self.client.post("/worker/resolve", json={"app_id": app_id, "fields": [f.model_dump() for f in fields]})
        r.raise_for_status()
        return r.json()

    def artifact(self, app_id: str, name: str, dest: Path) -> Path:
        r = self.client.get(f"/worker/artifact/{app_id}/{name}")
        r.raise_for_status()
        dest.write_bytes(r.content)
        return dest

    def result(self, task: dict, kind: str, result: dict, screenshot: Path | None) -> dict:
        payload = {"task_id": task["task_id"], "kind": kind, "app_id": task["app_id"], "result": result}
        files = [("json", (None, json.dumps(payload), "application/json"))]
        if screenshot and screenshot.exists():
            files.append(("screenshot", ("shot.png", screenshot.read_bytes(), "image/png")))
        r = self.client.post("/worker/result", files=files)
        r.raise_for_status()
        return r.json()


class Worker:
    def __init__(self, cfg: WorkerCfg, appliers: dict[str, ApplierCfg], core: Core,
                 page_factory: Callable[[str], Page], sleep: Callable[[float], None] = time.sleep):
        self.cfg = cfg
        self.appliers = appliers
        self.core = core
        self.page_factory = page_factory
        self.sleep = sleep

    # --- one task ------------------------------------------------------------------------

    def handle(self, task: dict) -> dict:
        platform = task["platform"]
        acfg = self.appliers.get(platform)
        tmp = Path(tempfile.mkdtemp(prefix="jobpipe-"))
        shot = tmp / "shot.png"
        if acfg is None:
            kind = "fill" if task["type"] == "apply.fill" else "submit"
            res = {"ok": False, "error": f"no applier for {platform}"} if kind == "fill" else \
                {"clicked": False, "error": f"no applier for {platform}"}
            return self.core.result(task, kind, res, None)
        page = self.page_factory(platform)
        try:
            applier = Applier(acfg, page)
            if task["type"] == "apply.fill":
                return self._fill(task, applier, page, tmp, shot)
            return self._submit(task, applier, page, tmp, shot)
        finally:
            page.close()

    def _prepare(self, task, applier: Applier, page: Page, tmp: Path):
        applier.open(task["url"])
        if applier.captcha() and not self._wait_captcha(applier, task):
            raise ApplyError("captcha or bot check: left for the owner, not bypassed")
        fields = applier.scan()
        resolved = self.core.resolve(task["app_id"], fields)
        files: dict[str, str] = {}
        for a in resolved["answers"]:
            v = a["value"]
            if a.get("source") == "cv" and v.startswith("artifact:"):
                app, name = v.removeprefix("artifact:").rsplit(":", 1)  # app ids contain colons
                files[v] = str(self.core.artifact(app, name, tmp / f"cv.{name}"))
            elif a.get("source") == "letter" and next((f for f in fields if f.key == a["key"] and f.kind == "file"),
                                                      None):
                p = tmp / "cover_letter.txt"
                p.write_text(v, encoding="utf-8")
                files[v] = str(p)
        applier.fill(fields, resolved["answers"], files)
        return fields, resolved

    def _fill(self, task, applier, page, tmp, shot) -> dict:
        try:
            _, resolved = self._prepare(task, applier, page, tmp)
            page.screenshot(str(shot))
            result = {"ok": True, "manifest_hash": resolved["hash"]}
        except ApplyError as e:
            _safe_shot(page, shot)
            result = {"ok": False, "error": str(e), "captcha": "captcha" in str(e)}
        return self.core.result(task, "fill", result, shot)

    def _submit(self, task, applier, page, tmp, shot) -> dict:
        try:
            fields, resolved = self._prepare(task, applier, page, tmp)
        except ApplyError as e:
            _safe_shot(page, shot)
            return self.core.result(task, "submit", {"clicked": False, "error": str(e)}, shot)
        page.screenshot(str(shot))
        missing = [u for u in resolved.get("unanswered", []) if u.get("required")]
        if resolved["hash"] != task.get("manifest_hash") or missing:
            # Never click with a different form or an empty required field: back to the owner.
            return self.core.result(task, "submit", {"clicked": False, "changed": True,
                                                     "new_fields": [f.model_dump() for f in fields]}, shot)
        if self.core.heartbeat({"state": "about to submit"}).get("paused"):
            return self.core.result(task, "submit", {"clicked": False, "error": "paused before the click"}, shot)
        try:
            confirmed = applier.submit()
        except ApplyError as e:
            _safe_shot(page, shot)
            return self.core.result(task, "submit", {"clicked": False, "error": str(e)}, shot)
        _safe_shot(page, shot)
        return self.core.result(task, "submit", {"clicked": True, "confirmed": confirmed}, shot)

    def _wait_captcha(self, applier: Applier, task: dict) -> bool:
        """Ask the owner (via heartbeat info, surfaced by the VM) and wait; never solve it ourselves."""
        self.core.heartbeat({"captcha": task["url"], "state": "waiting for the owner to solve a captcha"})
        waited = 0.0
        while waited < self.cfg.captcha_wait_s:
            self.sleep(10)
            waited += 10
            if not applier.captcha():
                return True
        return False

    # --- loop ----------------------------------------------------------------------------

    def ready_to_work(self) -> tuple[bool, str]:
        if self.cfg.require_ac and not system.on_ac_power():
            return False, "on battery"
        idle = system.idle_seconds()
        if idle < self.cfg.min_idle_s:
            return False, f"owner active ({int(idle)} s idle)"
        return True, "ok"

    def run_forever(self) -> None:  # pragma: no cover - the loop is exercised through handle()
        system.lower_priority()
        while True:
            try:
                ok, why = self.ready_to_work()
                hb = self.core.heartbeat({"ready": ok, "why": why})
                if hb.get("paused") or not ok:
                    self.sleep(60)
                    continue
                task = self.core.lease(wait=25)
                if task:
                    log.info("task %s %s", task["type"], task["app_id"])
                    self.handle(task)
            except httpx.HTTPError as e:
                log.warning("core unreachable: %s", type(e).__name__)
                self.sleep(30)


def _safe_shot(page: Page, shot: Path) -> None:
    with contextlib.suppress(Exception):
        page.screenshot(str(shot))
