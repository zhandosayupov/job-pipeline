"""Telegram cards for shortlisted jobs and what their buttons do (S2).

Callback data stays under Telegram's 64 bytes by using the jobs table rowid: `j:<action>:<rowid>`
for job cards and `r:<vote>:<rowid>` for label ratings.

Actions:
  go    create an application draft (idempotency key `apply:<company>:<job id>`); S3 tailors it
  skip  shortlisted -> skipped
  prot  add the company to protected.yaml and mark its live jobs protected
  draft (protected jobs only) same as go, but the application stays manual end to end
"""
from __future__ import annotations

import json
import random
import sqlite3
from pathlib import Path

from ..clock import Clock
from ..config.settings import AppConfig
from ..ports.notifier import Buttons, Notifier
from . import protected as prot
from .events import create_entity, record_event, transition
from .text import norm_company

TRACK_RU = {"hft": "HFT", "bigtech": "bigtech", "infra": "infra", "cpp": "C++", "backend": "backend",
            "local": "KZ", "other": "другое"}
VISA_RU = {"explicit": "виза ✓", "relocation_only": "релокация", "no": "без визы", "unknown": "виза ?"}
REMOTE_RU = {"kz": "remote из KZ ✓", "worldwide": "remote worldwide", "emea": "remote EMEA",
             "listed": "remote (страны списком)", "unknown": "", "n/a": "офис"}


def card_text(row: sqlite3.Row, label: dict) -> str:
    b = label.get("breakdown", {})
    lines = [
        ("🛡 " if row["protected"] else "") + f"{row['company']} — {row['title']}",
        " · ".join(x for x in (row["location"] or row["country"] or "", TRACK_RU.get(label.get("track"), ""),
                                VISA_RU.get(label.get("visa"), ""), REMOTE_RU.get(label.get("remote_scope"), ""))
                   if x),
        f"Оценка {label.get('score')}/100 (уровень {b.get('level')}, стек {b.get('stack')}, легальность "
        f"{b.get('legal')}, деньги {b.get('comp')}, свежесть {b.get('fresh')}) · {label.get('action')}",
    ]
    if label.get("reason"):
        lines.append(label["reason"])
    for key, title in (("visa_evidence", "Виза"), ("remote_evidence", "Remote")):
        if label.get(key):
            lines.append(f"{title}: «{label[key]}»")
    lines.append(row["url"])
    return "\n".join(lines)


def card_buttons(rowid: int, is_protected: bool) -> Buttons:
    first = ("🛠 Подготовить черновик", f"j:draft:{rowid}") if is_protected else ("✅ В работу", f"j:go:{rowid}")
    row2 = [("⏭ Пропустить", f"j:skip:{rowid}")]
    if not is_protected:
        row2.append(("🛡 Protect", f"j:prot:{rowid}"))
    return [[first], row2]


def send_cards(conn: sqlite3.Connection, notifier: Notifier, job_ids: list[str], min_score: int) -> int:
    """Instant cards for protected jobs and those scoring >= min_score; each job at most once."""
    sent = 0
    for jid in job_ids:
        row = conn.execute("SELECT rowid, * FROM jobs WHERE id = ?", (jid,)).fetchone()
        if row is None or row["state"] != "shortlisted":
            continue
        data = json.loads(row["data_json"] or "{}")
        label = data.get("label") or {}
        if data.get("card_sent") or not (row["protected"] or (label.get("score") or 0) >= min_score):
            continue
        if notifier.send(card_text(row, label), buttons=card_buttons(row["rowid"], bool(row["protected"]))):
            data["card_sent"] = True
            conn.execute("UPDATE jobs SET data_json = ? WHERE id = ?", (json.dumps(data, ensure_ascii=False), jid))
            sent += 1
    return sent


def handle_job_action(conn: sqlite3.Connection, clock: Clock, cfg: AppConfig, action: str, rowid: int) -> str:
    row = conn.execute("SELECT rowid, * FROM jobs WHERE rowid = ?", (rowid,)).fetchone()
    if row is None:
        return "Вакансия не найдена (возможно, удалена очисткой)."
    title = f"{row['company']} — {row['title']}"
    if action in ("go", "draft"):
        if row["state"] not in ("shortlisted", "labeled"):
            return f"{title}: сейчас в состоянии {row['state']}, в работу не беру."
        if row["protected"] and action == "go":
            return "Это protected-компания: только черновик вручную."
        key = f"apply:{norm_company(row['company']) or 'unknown'}:{row['id']}"
        if conn.execute("SELECT 1 FROM applications WHERE idempotency_key = ?", (key,)).fetchone():
            return f"{title}: заявка уже создана."
        create_entity(conn, clock, "application", f"app:{row['id']}", actor="owner",
                      columns={"job_id": row["id"], "idempotency_key": key},
                      data={"manual": bool(row["protected"]) or action == "draft"})
        return f"✅ {title}: заявка создана (черновик). Тейлоринг CV и письма — S3."
    if action == "skip":
        if row["state"] != "shortlisted":
            return f"{title}: уже {row['state']}."
        transition(conn, clock, "job", row["id"], "skipped", expected="shortlisted", actor="owner")
        return f"⏭ {title}: пропущена."
    if action == "prot":
        added = prot.protect(Path(cfg.paths.config_dir) / "protected.yaml", conn, clock, row["company"])
        return (f"🛡 {row['company']}: добавлена в protected." if added
                else f"🛡 {row['company']}: уже в protected.")
    return "Неизвестное действие."


# --- rating labels (S2 done-condition: owner agrees with >= 80% of 30 random labels) ----------

def next_rating_card(conn: sqlite3.Connection, rng: random.Random | None = None) -> tuple[str, Buttons] | None:
    rated = {r[0] for r in conn.execute("SELECT entity_id FROM events WHERE entity = 'job' AND kind = 'rated'")}
    rows = [r for r in conn.execute(
        "SELECT rowid, * FROM jobs WHERE state IN ('labeled', 'shortlisted', 'skipped') "
        "AND json_extract(data_json, '$.label') IS NOT NULL").fetchall() if r["id"] not in rated]
    if not rows:
        return None
    row = (rng or random.Random()).choice(rows)
    label = json.loads(row["data_json"])["label"]
    done = len(rated)
    text = f"Оценка разметки {done + 1}/30. Согласен с решением «{row['state']}»?\n\n" + card_text(row, label)
    return text, [[("👍 Согласен", f"r:up:{row['rowid']}"), ("👎 Нет", f"r:down:{row['rowid']}")]]


def record_rating(conn: sqlite3.Connection, clock: Clock, rowid: int, vote: str) -> str:
    row = conn.execute("SELECT id FROM jobs WHERE rowid = ?", (rowid,)).fetchone()
    if row is None:
        return "Вакансия не найдена."
    record_event(conn, clock, entity="job", entity_id=row["id"], kind="rated", actor="owner",
                 data={"vote": vote})
    up, total = agreement(conn)
    return f"Записал. Согласие: {up}/{total} ({round(100 * up / total)}%)."


def agreement(conn: sqlite3.Connection) -> tuple[int, int]:
    rows = conn.execute("SELECT data_json FROM events WHERE entity = 'job' AND kind = 'rated'").fetchall()
    votes = [json.loads(r[0]).get("vote") for r in rows]
    return votes.count("up"), len(votes)


# --- application approval cards (S3) --------------------------------------------------------


def application_card_text(conn: sqlite3.Connection, app_id: str) -> tuple[str, dict]:
    app = conn.execute("SELECT * FROM applications WHERE id = ?", (app_id,)).fetchone()
    job = conn.execute("SELECT * FROM jobs WHERE id = ?", (app["job_id"],)).fetchone()
    data = json.loads(app["data_json"] or "{}")
    arts = data.get("artifacts", {})
    plan = {}
    if arts.get("plan") and Path(arts["plan"]).exists():
        plan = json.loads(Path(arts["plan"]).read_text(encoding="utf-8"))
    letter = Path(arts["letter"]).read_text(encoding="utf-8") if arts.get("letter") else ""
    lines = [("🛡 " if job["protected"] else "") + f"Заявка: {job['company']} — {job['title']}", job["url"], ""]
    if plan.get("summary"):
        lines += [f"Summary: {plan['summary']}", ""]
    diff = plan.get("diff") or []
    if diff:
        lines.append("Отличия от канонического CV:")
        lines += [f"  {d[:150]}" for d in diff[:12]]
        if len(diff) > 12:
            lines.append(f"  …ещё {len(diff) - 12}")
        lines.append("")
    gaps = (plan.get("plan") or {}).get("gaps") or []
    if gaps:
        lines.append("Пробелы: " + "; ".join(gaps[:5]))
    if letter:
        lines += ["", "Письмо:", letter]
    for n in data.get("notes", [])[:5]:
        lines.append(f"ℹ️ {n}")
    return "\n".join(lines), arts


def send_application_card(conn: sqlite3.Connection, notifier: Notifier, app_id: str) -> bool:
    text, arts = application_card_text(conn, app_id)
    rowid = conn.execute("SELECT rowid FROM applications WHERE id = ?", (app_id,)).fetchone()[0]
    send_file = getattr(notifier, "send_file", None)
    cv = arts.get("pdf") or arts.get("tex")
    if cv and send_file:
        send_file(cv, caption="CV " + ("(PDF)" if arts.get("pdf") else "(LaTeX: tectonic не установлен)"))
    return notifier.send(text, buttons=[[("✅ Одобрить", f"a:ok:{rowid}"), ("🔁 Переделать", f"a:redo:{rowid}")],
                                        [("🗑 Отменить", f"a:cancel:{rowid}")]])


def handle_app_action(conn: sqlite3.Connection, clock: Clock, action: str, rowid: int) -> str:
    app = conn.execute("SELECT rowid, * FROM applications WHERE rowid = ?", (rowid,)).fetchone()
    if app is None:
        return "Заявка не найдена."
    data = json.loads(app["data_json"] or "{}")
    if action == "ok":
        if app["state"] != "ready":
            return f"Заявка в состоянии {app['state']}: одобрять нечего."
        data["approved_cv"] = True
        conn.execute("UPDATE applications SET data_json = ? WHERE id = ?", (json.dumps(data, ensure_ascii=False),
                                                                           app["id"]))
        record_event(conn, clock, entity="application", entity_id=app["id"], kind="approved", actor="owner",
                     data={"what": "cv_letter"})
        return "✅ CV и письмо одобрены. Подача — S4 (по политике автономии)."
    if action == "redo":
        data.pop("approved_cv", None)
        data["blocked"] = []
        conn.execute("UPDATE applications SET data_json = ? WHERE id = ?", (json.dumps(data, ensure_ascii=False),
                                                                           app["id"]))
        if app["state"] == "ready":
            transition(conn, clock, "application", app["id"], "tailoring", expected="ready", actor="owner")
            transition(conn, clock, "application", app["id"], "draft", expected="tailoring", actor="owner")
        elif app["state"] != "draft":
            return f"Заявка в состоянии {app['state']}: переделать нельзя."
        return "🔁 Переделаю при следующем запуске тейлоринга (раз в час) или командой `jobpipe run tailor`."
    if action == "cancel":
        if app["state"] in ("withdrawn", "rejected", "offer"):
            return f"Заявка уже {app['state']}."
        transition(conn, clock, "application", app["id"], "withdrawn", actor="owner")
        return "🗑 Заявка отменена."
    return "Неизвестное действие."
