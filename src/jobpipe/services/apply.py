"""Applying (S4, ADR 0007/0016): the VM side of the browser worker.

  ready (+ approved CV) --schedule--> filling --fill result--> decide_apply():
      submit    -> side-effect key reserved -> submitting --submit result--> applied | submit_unknown | apply_failed
      ask       -> awaiting_approval (card: screenshot, answers, reasons) --Approve--> submitting
      dry_run   -> awaiting_approval with a dry-run report --OK--> ready
      defer     -> ready (cap reached or paused; tried again later)
      block     -> withdrawn (already applied / truth gate)
  A submit that finds a different form than the approved one (manifest hash) goes back to awaiting_approval.

The worker never decides: it scans, fills what the VM resolved, screenshots and clicks only when told.
Every submit reserves `apply:<company>:<job id>` in side_effects first (at most once, ADR 0014).
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from ..clock import Clock, iso
from ..config.settings import AppConfig
from ..ports.notifier import Notifier
from . import control
from .events import record_event, transition
from .forms import FormField, Resolution, resolve
from .kb import KnowledgeBase
from .policy import ApplyFacts, Route, decide_apply
from .queue import Queue
from .text import norm_company

log = logging.getLogger(__name__)
FILL, SUBMIT = "apply.fill", "apply.submit"


class FillResult(BaseModel):
    ok: bool
    error: str | None = None
    captcha: bool = False
    manifest_hash: str | None = None
    screenshot: str | None = None          # path on the VM after upload


class SubmitResult(BaseModel):
    clicked: bool = False                   # the submit button was pressed
    confirmed: bool = False                 # a confirmation page/text was seen afterwards
    changed: bool = False                   # the form differs from the approved manifest: not clicked
    error: str | None = None
    screenshot: str | None = None
    new_fields: list[FormField] = Field(default_factory=list)


def _on(host: str, *domains: str) -> bool:
    """host is one of the domains or a subdomain of one: exact labels, never a substring (CodeQL)."""
    return any(host == d or host.endswith("." + d) for d in domains)


def platform_of(url: str, source: str | None = None) -> str | None:
    host = (urlsplit(url).hostname or "").lower()
    if _on(host, "greenhouse.io"):
        return "greenhouse"
    if _on(host, "jobs.lever.co"):
        return "lever"
    if _on(host, "ashbyhq.com"):
        return "ashby"
    if _on(host, "hh.kz", "hh.ru"):
        return "hh"
    return source if source in {"greenhouse", "lever", "ashby"} else None


def _app_data(conn, app_id) -> dict:
    return json.loads(conn.execute("SELECT data_json FROM applications WHERE id = ?", (app_id,)).fetchone()[0] or "{}")


def _save(conn, app_id, data) -> None:
    conn.execute("UPDATE applications SET data_json = ? WHERE id = ?", (json.dumps(data, ensure_ascii=False), app_id))


def _job(conn, app_id) -> sqlite3.Row:
    return conn.execute("SELECT j.* FROM jobs j JOIN applications a ON a.job_id = j.id WHERE a.id = ?",
                        (app_id,)).fetchone()


def side_effect_key(conn, app_id) -> str:
    return conn.execute("SELECT idempotency_key FROM applications WHERE id = ?", (app_id,)).fetchone()[0]


# --- 1. schedule ---------------------------------------------------------------------------


def schedule(conn: sqlite3.Connection, clock: Clock, cfg: AppConfig, queue: Queue, notifier: Notifier,
             limit: int = 20) -> list[str]:
    """Send ready (and approved) applications to the worker. Returns the app ids scheduled."""
    if control.is_paused(conn):
        return []
    now = iso(clock.now())
    out = []
    rows = conn.execute("SELECT rowid, * FROM applications WHERE state = 'ready' ORDER BY updated_at LIMIT ?",
                        (limit,)).fetchall()
    for app in rows:
        data = json.loads(app["data_json"] or "{}")
        if cfg.policy.require_cv_approval and not data.get("approved_cv"):
            continue
        if data.get("deferred_until") and data["deferred_until"] > now:
            continue
        if data.get("dry_run_done") and control.is_dry_run(conn, data.get("platform", "")):
            continue    # one dry-run report per application until dry run is switched off (S4 review)
        job = _job(conn, app["id"])
        platform = platform_of(job["url"], job["source"])
        if platform not in cfg.policy.apply_platforms:
            if not data.get("manual_notified"):
                notifier.send(f"Подай вручную: {job['company']} — {job['title']}\n{job['url']}\n"
                              f"(платформа {platform or 'неизвестна'}: автоподачи нет). CV и письмо в карточке выше.")
                data["manual_notified"] = True
                _save(conn, app["id"], data)
            continue
        data["platform"] = platform
        data["attempt"] = data.get("attempt", 0) + 1
        _save(conn, app["id"], data)
        transition(conn, clock, "application", app["id"], "filling", expected="ready", actor="apply")
        queue.enqueue(FILL, {"app_id": app["id"], "platform": platform, "url": job["url"]},
                      idempotency_key=f"fill:{app['id']}:{data['attempt']}", max_attempts=2)
        out.append(app["id"])
    return out


# --- 2. resolve the scanned form ----------------------------------------------------------------


def resolve_for(conn: sqlite3.Connection, cfg: AppConfig, kb: KnowledgeBase, app_id: str,
                fields: list[FormField]) -> Resolution:
    job = _job(conn, app_id)
    data = _app_data(conn, app_id)
    arts = data.get("artifacts", {})
    letter = Path(arts["letter"]).read_text(encoding="utf-8") if arts.get("letter") else None
    job_data = json.loads(job["data_json"] or "{}")
    res = resolve(fields, kb, cfg.policy, job_country=job["country"], lang=job_data.get("lang", "en"),
                  cv_path=f"artifact:{app_id}:{'pdf' if arts.get('pdf') else 'tex'}", letter=letter,
                  posting_salary=job_data.get("salary"))
    data["form"] = {
        "manifest": res.manifest(), "hash": res.manifest_hash(),
        "unanswered": [f.model_dump() for f in res.unanswered],
        "topics_asked": sorted(res.topics_asked), "topics_preapproved": sorted(res.topics_preapproved),
    }
    _save(conn, app_id, data)
    return res


# --- 3. fill result -> decision -----------------------------------------------------------------


def _applied_today(conn, clock, cfg, platform) -> int:
    start = clock.now().astimezone(ZoneInfo(cfg.settings.timezone)).replace(hour=0, minute=0, second=0, microsecond=0)
    rows = conn.execute(
        "SELECT a.data_json, a.id FROM events e JOIN applications a ON a.id = e.entity_id "
        "WHERE e.entity = 'application' "
        "AND e.to_state = 'submitting' AND e.ts >= ?", (iso(start),)).fetchall()
    return len({r[1] for r in rows if json.loads(r[0] or "{}").get("platform") == platform})   # distinct apps


def _similar(conn, app_id, job) -> bool:
    key = norm_company(job["company"])
    rows = conn.execute(
        "SELECT a.id, j.company FROM applications a JOIN jobs j ON j.id = a.job_id WHERE a.id != ? AND a.state IN "
        "('submitting','applied','submit_unknown','replied','screening','interview','offer')", (app_id,)).fetchall()
    return any(norm_company(r["company"]) == key for r in rows)


def facts_for(conn, clock, cfg, app_id) -> ApplyFacts:
    data = _app_data(conn, app_id)
    job = _job(conn, app_id)
    form = data.get("form", {})
    platform = data.get("platform", "")
    status = control.SideEffects(conn, clock).status(side_effect_key(conn, app_id))
    return ApplyFacts(
        platform=platform, protected=bool(job["protected"]) or bool(data.get("manual")),
        dry_run=control.is_dry_run(conn, platform), paused=control.is_paused(conn),
        already_applied=status in ("done", "unknown", "planned"), truth_ok=not data.get("blocked"),
        unanswered_questions=len(form.get("unanswered", [])), similar_role_warning=_similar(conn, app_id, job),
        applied_today=_applied_today(conn, clock, cfg, platform),
        topics_asked=frozenset(form.get("topics_asked", [])),
        topics_preapproved=frozenset(form.get("topics_preapproved", [])),
        job_country=job["country"], posting_salary_known=bool(json.loads(job["data_json"] or "{}").get("salary")),
    )


def on_fill_result(conn: sqlite3.Connection, clock: Clock, cfg: AppConfig, queue: Queue, notifier: Notifier,
                   app_id: str, r: FillResult) -> Route | None:
    job = _job(conn, app_id)
    data = _app_data(conn, app_id)
    title = f"{job['company']} — {job['title']}"
    if r.screenshot:
        data.setdefault("screenshots", []).append(r.screenshot)
        _save(conn, app_id, data)
    if not r.ok:
        transition(conn, clock, "application", app_id, "apply_failed", expected="filling", actor="worker",
                   data={"error": (r.error or "")[:300], "captcha": r.captcha})
        _send(notifier, ("🧩 Капча/проверка: " if r.captcha else "⚠️ Не удалось заполнить форму: ") + title
              + f"\n{r.error or ''}\n{job['url']}", r.screenshot)
        return None
    d = decide_apply(cfg.policy, facts_for(conn, clock, cfg, app_id))
    data = _app_data(conn, app_id)
    data["decision"] = {"route": d.route.value, "reasons": d.reasons}
    _save(conn, app_id, data)
    rowid = conn.execute("SELECT rowid FROM applications WHERE id = ?", (app_id,)).fetchone()[0]
    if d.route is Route.SUBMIT:
        _start_submit(conn, clock, queue, app_id, expected="filling")
    elif d.route is Route.ASK:
        transition(conn, clock, "application", app_id, "awaiting_approval", expected="filling", actor="apply",
                   data={"reasons": d.reasons})
        _send(notifier, _approval_text(title, job["url"], data, d.reasons), r.screenshot,
              buttons=[[("🚀 Отправить", f"s:ok:{rowid}"), ("✋ Не отправлять", f"s:no:{rowid}")]])
    elif d.route is Route.DRY_RUN:
        transition(conn, clock, "application", app_id, "awaiting_approval", expected="filling", actor="apply",
                   data={"dry_run": True})
        _send(notifier, "🧪 DRY RUN — форма заполнена, но НЕ отправлена:\n" + _approval_text(title, job["url"], data,
              d.reasons), r.screenshot, buttons=[[("👌 Понятно", f"s:dry:{rowid}")]])
    elif d.route is Route.DEFER:
        if not control.is_paused(conn):        # a pause just waits for /resume; the daily cap waits 12 h
            data["deferred_until"] = iso(clock.now() + timedelta(hours=12))
            _save(conn, app_id, data)
        transition(conn, clock, "application", app_id, "ready", expected="filling", actor="apply",
                   data={"deferred": d.reasons})
    else:  # BLOCK
        transition(conn, clock, "application", app_id, "withdrawn", expected="filling", actor="apply",
                   data={"blocked": d.reasons})
        notifier.send(f"⛔ Подача остановлена: {title}\n" + "\n".join(d.reasons))
    return d.route


def _start_submit(conn, clock, queue: Queue, app_id: str, expected: str) -> bool:
    key = side_effect_key(conn, app_id)
    se = control.SideEffects(conn, clock)
    if not se.plan(key, "submit", {"app_id": app_id}):
        return False  # already reserved: never submit twice
    data = _app_data(conn, app_id)
    data["submit_attempt"] = data.get("submit_attempt", 0) + 1  # a returned form may be approved again
    _save(conn, app_id, data)
    transition(conn, clock, "application", app_id, "submitting", expected=expected, actor="apply")
    queue.enqueue(SUBMIT, {"app_id": app_id, "platform": data.get("platform"), "url": _job(conn, app_id)["url"],
                           "manifest_hash": data.get("form", {}).get("hash")},
                  idempotency_key=f"submit:{app_id}:{data['submit_attempt']}", max_attempts=1)
    return True


def _approval_text(title: str, url: str, data: dict, reasons: list[str]) -> str:
    form = data.get("form", {})
    lines = [f"Заявка: {title}", url, ""]
    if reasons:
        lines.append("Почему нужен ты: " + "; ".join(reasons))
    lines.append("Заполнено:")
    for m in form.get("manifest", [])[:20]:
        v = m["value"] if not m["value"].startswith("artifact:") else "[файл CV]"
        lines.append(f"  {m['key']}: {v[:80]}")
    if form.get("unanswered"):
        lines.append("Без ответа: " + "; ".join(f["label"][:80] for f in form["unanswered"]))
    return "\n".join(lines)


def _send(notifier, text, screenshot=None, buttons=None) -> None:
    if screenshot and getattr(notifier, "send_file", None):
        notifier.send_file(screenshot, caption="Скриншот формы")
    notifier.send(text, buttons=buttons)


# --- 4. owner decisions on cards --------------------------------------------------------------


def owner_decision(conn: sqlite3.Connection, clock: Clock, queue: Queue, action: str, rowid: int) -> str:
    app = conn.execute("SELECT id, state, data_json FROM applications WHERE rowid = ?", (rowid,)).fetchone()
    if app is None:
        return "Заявка не найдена."
    if app["state"] != "awaiting_approval":
        return f"Заявка в состоянии {app['state']}: решение уже принято."
    if action == "ok":
        if control.is_paused(conn):
            return "Пауза включена: сначала /resume."
        if not _start_submit(conn, clock, queue, app["id"], expected="awaiting_approval"):
            return "Эта вакансия уже подавалась (ключ занят): второй раз не отправляю."
        record_event(conn, clock, entity="application", entity_id=app["id"], kind="approved", actor="owner",
                     data={"what": "submit"})
        unanswered = [f["label"] for f in json.loads(app["data_json"] or "{}").get("form", {}).get("unanswered", [])
                      if f.get("required")]
        if unanswered:
            return ("🚀 В очереди, но в форме есть обязательные вопросы без ответа: " + "; ".join(unanswered)[:300]
                    + ". Добавь ответы в kb/answers.yaml: воркер перечитает форму перед кликом и без ответов "
                    "не отправит, а вернёт заявку тебе.")
        return "🚀 Отправка поставлена в очередь воркера."
    if action == "dry":
        data = _app_data(conn, app["id"])
        data["dry_run_done"] = True
        _save(conn, app["id"], data)
        transition(conn, clock, "application", app["id"], "ready", expected="awaiting_approval", actor="owner")
        return "👌 Ок. Выключи dry run для платформы (/dryrun <платформа> off), чтобы отправлять по-настоящему."
    if action == "no":
        transition(conn, clock, "application", app["id"], "withdrawn", expected="awaiting_approval", actor="owner")
        return "✋ Не отправляю. Заявка отменена."
    return "Неизвестное действие."


def submit_lost(conn: sqlite3.Connection, clock: Clock, notifier: Notifier | None, app_id: str) -> None:
    """A submit task came back from an expired lease: the outcome is unknown, never retried."""
    key = side_effect_key(conn, app_id)
    se = control.SideEffects(conn, clock)
    if se.status(key) == "planned":            # never overwrite a known outcome (done/failed) (S4 review)
        se.mark(key, "unknown")
    if conn.execute("SELECT state FROM applications WHERE id = ?", (app_id,)).fetchone()[0] == "submitting":
        transition(conn, clock, "application", app_id, "submit_unknown", expected="submitting", actor="apply",
                   data={"error": "worker lease expired during submit"})
    job = _job(conn, app_id)
    if notifier is not None:
        notifier.send(f"❓ Воркер пропал во время отправки: {job['company']} — {job['title']}\n{job['url']}\n"
                      f"Клик мог произойти. Проверь на сайте: jobpipe effects resolve {key} done|failed")


# --- 5. submit result ---------------------------------------------------------------------------


def on_submit_result(conn: sqlite3.Connection, clock: Clock, cfg: AppConfig, kb: KnowledgeBase | None,
                     notifier: Notifier, app_id: str, r: SubmitResult) -> str:
    job = _job(conn, app_id)
    title = f"{job['company']} — {job['title']}"
    key = side_effect_key(conn, app_id)
    se = control.SideEffects(conn, clock)
    data = _app_data(conn, app_id)
    if r.screenshot:
        data.setdefault("screenshots", []).append(r.screenshot)
        _save(conn, app_id, data)
    if r.changed and not r.clicked:
        se.resolve(key, "reset", actor="apply")  # nothing went out
        if kb is not None and r.new_fields:
            resolve_for(conn, cfg, kb, app_id, r.new_fields)
        transition(conn, clock, "application", app_id, "awaiting_approval", expected="submitting", actor="worker",
                   data={"reason": "form changed"})
        rowid = conn.execute("SELECT rowid FROM applications WHERE id = ?", (app_id,)).fetchone()[0]
        _send(notifier, "🔄 Форма изменилась с момента заполнения, проверь заново:\n"
              + _approval_text(title, job["url"], _app_data(conn, app_id), []), r.screenshot,
              buttons=[[("🚀 Отправить", f"s:ok:{rowid}"), ("✋ Не отправлять", f"s:no:{rowid}")]])
        return "changed"
    if not r.clicked:
        se.resolve(key, "reset", actor="apply")  # failed before the click: nothing went out, may retry
        transition(conn, clock, "application", app_id, "apply_failed", expected="submitting", actor="worker",
                   data={"error": (r.error or "")[:300]})
        _send(notifier, f"⚠️ Не отправлено: {title}\n{r.error or ''}\n{job['url']}", r.screenshot)
        return "failed"
    if r.confirmed:
        se.mark(key, "done")
        transition(conn, clock, "application", app_id, "applied", expected="submitting", actor="worker")
        _send(notifier, f"✅ Отправлено: {title}\n{job['url']}", r.screenshot)
        return "applied"
    se.mark(key, "unknown")
    transition(conn, clock, "application", app_id, "submit_unknown", expected="submitting", actor="worker",
               data={"error": (r.error or "")[:300]})
    _send(notifier, f"❓ Нажал «отправить», но подтверждения не видно: {title}\n{job['url']}\n"
          f"Проверь на сайте и реши: jobpipe effects resolve {key} done|failed", r.screenshot)
    return "unknown"


def workers_status(conn: sqlite3.Connection, clock: Clock, stale_s: int = 180) -> list[tuple[str, bool, str]]:
    """(worker id, online, last_seen) for /status."""
    cutoff = iso(clock.now() - timedelta(seconds=stale_s))
    return [(r["id"], r["last_seen"] >= cutoff, r["last_seen"])
            for r in conn.execute("SELECT id, last_seen FROM workers ORDER BY id").fetchall()]
