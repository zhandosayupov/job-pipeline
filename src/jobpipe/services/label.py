"""`jobpipe run label`: LLM labeling in batches, then shortlist (spec §7).

* Jobs in state `prefiltered` are labeled in batches of `label.batch_size` through the LLM router
  (task `label`), most promising first, at most `label.max_jobs_per_run` per run.
* Budget guard: at most `label.daily_max_jobs` labels per day (Almaty); when reached, labeling stops
  and the owner is told once.
* The LLM output is checked before it is trusted: breakdown values are clamped to the rubric, the
  freshness part is computed here from `posted_at` (not by the LLM), and every evidence quote must
  appear in the posting, else the field falls back to "unknown" (unknown stays null).
* Then: score >= `shortlist_threshold` and action != skip -> `shortlisted`, else `skipped`.
  Protected companies are always shortlisted: they go to the owner, never further on their own.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator

from ..clock import Clock, iso
from ..config.settings import AppConfig
from ..ports.notifier import Notifier
from . import control
from .events import transition
from .llm_router import LLMUnavailable

log = logging.getLogger(__name__)
BUDGET_FLAG = "label.budget_notified"


class Breakdown(BaseModel):
    level: int = 0
    stack: int = 0
    legal: int = 0
    comp: int = 0


class JobLabel(BaseModel):
    id: str
    breakdown: Breakdown
    track: Literal["hft", "bigtech", "infra", "cpp", "backend", "local", "other"] = "other"
    level: Literal["junior", "mid", "senior", "staff", "unknown"] = "unknown"
    visa: Literal["explicit", "relocation_only", "no", "unknown"] = "unknown"
    visa_evidence: str | None = None
    remote_scope: Literal["kz", "worldwide", "emea", "listed", "unknown", "n/a"] = "unknown"
    remote_evidence: str | None = None
    resume_variant: Literal["hft", "backend", "generic", "ru"] = "generic"
    action: Literal["apply_now", "maybe", "skip"] = "maybe"
    reason: str = Field("", max_length=400)

    @field_validator("track", "level", "visa", "remote_scope", "resume_variant", "action", mode="before")
    @classmethod
    def _known_or_default(cls, v, info):
        """An unexpected value becomes the field's safe default instead of rejecting the whole batch
        of 20 (live: Gemini returned resume_variant values outside the list). For visa and remote the
        default is "unknown", which is the safe direction; for action it is "maybe"."""
        v = v.strip().lower().replace(" ", "_").replace("-", "_") if isinstance(v, str) else v
        allowed = cls.model_fields[info.field_name].annotation.__args__
        return v if v in allowed else cls.model_fields[info.field_name].default

    @field_validator("reason", mode="before")
    @classmethod
    def _short_reason(cls, v):
        return v[:400] if isinstance(v, str) else v


class LabelBatch(BaseModel):
    labels: list[JobLabel]


LIMITS = {"level": 25, "stack": 25, "legal": 25, "comp": 15}
LEGAL_UNKNOWN = 8          # prompts/label/v1.md: "unknown = 8"
# The rubric's best value per claim (prompts/label/v1.md, "legal"): sponsorship with a named route 25,
# relocation support 15; remote open to KZ 24, worldwide 22, an aggregator's remote label 18, EMEA 12;
# an office job with no visa statement 10. "no" (refuses sponsorship) is 0 unless remote says more.
VISA_CAP = {"explicit": 25, "relocation_only": 15, "no": 0}
REMOTE_CAP = {"kz": 24, "worldwide": 22, "listed": 18, "emea": 12, "n/a": 10}
MAX_MISSES = 3             # a job the model leaves out of its answer this many times is dropped


def fresh_points(posted_at: str | None, now: datetime) -> int:
    """Freshness (0-10) is computed, not asked: <=3d 10, <=7d 7, <=14d 4, <=30d 2, else 0; unknown 2."""
    if not posted_at:
        return 2
    try:
        dt = datetime.fromisoformat(posted_at.replace("Z", "+00:00"))
    except ValueError:
        return 2
    days = (now - dt).total_seconds() / 86400
    for limit, pts in ((3, 10), (7, 7), (14, 4), (30, 2)):
        if days <= limit:
            return pts
    return 0


def _norm(s: str) -> str:
    return " ".join(re.sub(r"[^\w%€$£₸+#.,-]+", " ", s.lower()).split())


def evidence_ok(quote: str | None, text: str) -> bool:
    """A quote counts only if it is really in the posting (whitespace and punctuation tolerant)."""
    if not quote:
        return False
    q = _norm(quote)
    return bool(q) and len(q.split()) <= 25 and q in _norm(text)


def verify(label: JobLabel, text: str, posted_at: str | None, now: datetime) -> dict:
    """Clamp, recompute and check a label; returns the dict stored in jobs.data_json['label']."""
    b = {k: max(0, min(LIMITS[k], getattr(label.breakdown, k))) for k in LIMITS}
    b["fresh"] = fresh_points(posted_at, now)
    out = label.model_dump()
    out["breakdown"] = b
    out["score"] = sum(b.values())
    if not evidence_ok(label.visa_evidence, text):
        out["visa_evidence"] = None
        if label.visa != "unknown":           # every visa claim needs a quote, not only the strong ones
            out["visa"] = "unknown"
            out["unverified"] = [*out.get("unverified", []), "visa"]
    if not evidence_ok(label.remote_evidence, text):
        out["remote_evidence"] = None
        if label.remote_scope not in ("unknown", "n/a"):
            out["remote_scope"] = "unknown"
            out["unverified"] = [*out.get("unverified", []), "remote_scope"]
    # The legal points come from the visa and remote claims, so they can be worth at most what the
    # rubric gives the claims that survived the quote check (S2 review): a rejected sponsorship claim
    # next to a quoted "remote EMEA" is worth 12, not 25; nothing left is worth the "unknown" 8.
    b["legal"] = min(b["legal"], legal_cap(out["visa"], out["remote_scope"]))
    out["score"] = sum(b.values())
    return out


def legal_cap(visa: str, remote_scope: str) -> int:
    """Highest `legal` value prompts/label/v1.md allows for these (verified) values."""
    caps = [c for c in (VISA_CAP.get(visa), REMOTE_CAP.get(remote_scope)) if c is not None]
    return max(caps) if caps else LEGAL_UNKNOWN


@dataclass
class LabelResult:
    labeled: int = 0
    shortlisted: int = 0
    skipped: int = 0
    protected: int = 0
    failed_batches: int = 0
    budget_stop: bool = False
    shortlisted_ids: list[str] = field(default_factory=list)


def _queue_order(row: sqlite3.Row) -> tuple:
    data = json.loads(row["data_json"] or "{}")
    pri = 0
    pri += 3 if row["protected"] else 0
    pri += {"explicit": 3, "relocation_only": 2}.get(data.get("visa") or "", 0)
    pri += 2 if data.get("remote") == "remote" else 0
    pri += 2 if "c++" in (row["title"] or "").lower() else 0
    pri += 1 if row["kind"] == "ats" else 0
    return (pri, row["posted_at"] or "")


def _labels_today(conn: sqlite3.Connection, clock: Clock, tz: str) -> int:
    start = clock.now().astimezone(ZoneInfo(tz)).replace(hour=0, minute=0, second=0, microsecond=0)
    return conn.execute(
        "SELECT COUNT(*) FROM events WHERE entity = 'job' AND kind = 'transition' AND to_state = 'labeled' "
        "AND ts >= ?", (iso(start),)).fetchone()[0]


RATE_WAIT_S = 61
MAX_RATE_WAITS = 20          # at most ~20 minutes of waiting per run (the unit allows 30)


def _rate_limited_only(e: LLMUnavailable) -> bool:
    return bool(e.attempts) and all("rpm quota" in a or "tpm quota" in a for a in e.attempts)


def run(conn: sqlite3.Connection, clock: Clock, cfg: AppConfig, router, notifier: Notifier,
        limit: int | None = None, sleep=time.sleep) -> LabelResult:
    lc = cfg.settings.label
    res = LabelResult()
    if control.is_paused(conn):
        return res
    today = clock.now().astimezone(ZoneInfo(cfg.settings.timezone)).date().isoformat()
    left = lc.daily_max_jobs - _labels_today(conn, clock, cfg.settings.timezone)
    if left <= 0:
        res.budget_stop = True
        if control.get_flag(conn, BUDGET_FLAG) != today:
            notifier.send(f"Разметка остановлена до завтра: дневной лимит {lc.daily_max_jobs} вакансий исчерпан.")
            control.set_flag(conn, clock, BUDGET_FLAG, today, actor="label")
        return res
    rows = conn.execute(
        "SELECT id, company, title, location, country, kind, posted_at, url, protected, data_json FROM jobs "
        "WHERE state = 'prefiltered'").fetchall()
    rate_waits = 0
    rows = sorted(rows, key=_queue_order, reverse=True)[: min(limit or lc.max_jobs_per_run, left)]
    now = clock.now()
    for i in range(0, len(rows), lc.batch_size):
        if control.is_paused(conn):
            break
        batch = rows[i: i + lc.batch_size]
        try:
            labels = _label_batch(cfg, router, batch)
        except LLMUnavailable as e:
            if not _rate_limited_only(e) or rate_waits >= MAX_RATE_WAITS:
                res.failed_batches += 1
                log.warning("label batch failed", extra={"data": {"error": str(e)[:300]}})
                continue
            # Every target was only over its per-minute quota (free Gemini: 10/min): wait for the
            # window instead of dropping the batch (live: 6 of 15 batches lost this way).
            rate_waits += 1
            sleep(RATE_WAIT_S)
            try:
                labels = _label_batch(cfg, router, batch)
            except LLMUnavailable as e2:
                res.failed_batches += 1
                log.warning("label batch failed", extra={"data": {"error": str(e2)[:300]}})
                continue
        for row in batch:
            lab = labels.get(row["id"])
            if lab is None:
                # stays prefiltered and is retried, but not forever (S2 review: tokens every run)
                data = json.loads(row["data_json"] or "{}")
                data["label_misses"] = int(data.get("label_misses", 0)) + 1
                conn.execute("UPDATE jobs SET data_json = ? WHERE id = ?",
                             (json.dumps(data, ensure_ascii=False), row["id"]))
                if data["label_misses"] >= MAX_MISSES:
                    transition(conn, clock, "job", row["id"], "dropped", expected="prefiltered", actor="label",
                               data={"rule": "label_missing", "misses": data["label_misses"]})
                    conn.execute("UPDATE jobs SET drop_rule = 'label_missing' WHERE id = ?", (row["id"],))
                continue
            data = json.loads(row["data_json"] or "{}")
            text = "\n".join(filter(None, [row["title"], row["location"], data.get("remote_text"),
                                           data.get("description")]))
            checked = verify(lab, text, row["posted_at"], now)
            data["label"] = checked
            conn.execute("UPDATE jobs SET data_json = ? WHERE id = ?",
                         (json.dumps(data, ensure_ascii=False), row["id"]))
            transition(conn, clock, "job", row["id"], "labeled", expected="prefiltered", actor="label",
                       data={"score": checked["score"], "action": checked["action"]})
            res.labeled += 1
            keep = row["protected"] or (checked["score"] >= lc.shortlist_threshold and checked["action"] != "skip")
            if keep:
                transition(conn, clock, "job", row["id"], "shortlisted", expected="labeled", actor="label")
                res.shortlisted += 1
                res.protected += 1 if row["protected"] else 0
                res.shortlisted_ids.append(row["id"])
            else:
                transition(conn, clock, "job", row["id"], "skipped", expected="labeled", actor="label")
                res.skipped += 1
    return res


def _label_batch(cfg: AppConfig, router, batch: list[sqlite3.Row]) -> dict[str, JobLabel]:
    """One LLM call for up to batch_size jobs. Short ids (j1..jN) keep the prompt small and stop the
    model from inventing ids; they are mapped back here."""
    items, back = [], {}
    for n, r in enumerate(batch, 1):
        data = json.loads(r["data_json"] or "{}")
        key = f"j{n}"
        back[key] = r["id"]
        items.append({
            "id": key, "title": r["title"], "company": r["company"], "location": r["location"],
            "country": r["country"], "remote": data.get("remote_text"), "salary": data.get("salary"),
            "posted": (r["posted_at"] or "")[:10], "text": data.get("excerpt") or "",
        })
    result = router.complete("label", {
        "profile": cfg.settings.label.profile,
        "work_authorized": ", ".join(cfg.policy.work_authorized_countries),
        "jobs": json.dumps(items, ensure_ascii=False),
    }, schema=LabelBatch)
    out: dict[str, JobLabel] = {}
    for lab in result.data.labels:
        jid = back.get(lab.id.strip())
        if jid and jid not in out:
            out[jid] = lab
    return out


def score_of(data_json: str | None) -> int | None:
    lab = (json.loads(data_json or "{}") or {}).get("label") or {}
    return lab.get("score")
