"""`jobpipe run collect`: fetch every enabled source, store new postings, prefilter, dedupe, detect
closures, record per-source stats and alert on unhealthy sources.

Each source is isolated: one failing source never stops the others.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field

from ..adapters.http import Http
from ..adapters.sources import ATS_SOURCES, SOURCES, FetchOutcome, SourceContext
from ..adapters.storage.db import tx
from ..clock import Clock, iso
from ..config.settings import AppConfig
from ..domain import states
from ..domain.job import KIND_RANK, Posting
from ..ports.notifier import Notifier
from . import control
from . import text as T
from .events import create_entity, transition
from .prefilter import decide

log = logging.getLogger(__name__)
LIVE_STATES = ("prefiltered", "labeled", "shortlisted")
DESCRIPTION_CHARS = 8000


@dataclass
class SourceResult:
    source: str
    status: str
    fetched: int = 0
    new: int = 0
    kept: int = 0
    dropped: int = 0
    closed: int = 0
    errors: list[str] = field(default_factory=list)


class Collector:
    def __init__(
        self,
        conn: sqlite3.Connection,
        clock: Clock,
        cfg: AppConfig,
        http: Http,
        notifier: Notifier,
        registry: dict[str, type] | None = None,
    ):
        self.conn = conn
        self.clock = clock
        self.cfg = cfg
        self.http = http
        self.notifier = notifier
        self.registry = registry or SOURCES

    def run(self, only: list[str] | None = None) -> list[SourceResult]:
        if control.is_paused(self.conn):
            log.info("paused; collect skipped")
            return []
        run_id = control.start_run(self.conn, self.clock, "run collect")
        results: list[SourceResult] = []
        status = "ok"
        try:
            for name, scfg in self.cfg.sources.sources.items():
                if only and name not in only:
                    continue
                if not only and not scfg.enabled:
                    continue
                if control.is_paused(self.conn):
                    status = "stopped"
                    break
                results.append(self._run_source(name))
        except Exception:
            status = "failed"
            raise
        finally:
            control.finish_run(self.conn, self.clock, run_id, status, {r.source: r.status for r in results})
        return results

    # --- one source ----------------------------------------------------------------------

    def _run_source(self, name: str) -> SourceResult:
        started = iso(self.clock.now())
        cls = self.registry.get(name)
        if cls is None:
            res = SourceResult(name, "error", errors=[f"unknown source {name!r}"])
            self._record_run(res, started)
            return res
        scfg = self.cfg.sources.sources[name]
        companies = (
            [c for c in self.cfg.companies.companies if c.ats == name and c.enabled] if name in ATS_SOURCES else []
        )
        ctx = SourceContext(params=dict(scfg.params), companies=companies, cursors=self._cursors(name))
        try:
            outcome: FetchOutcome = cls().fetch(self.http, ctx)
        except Exception as e:  # a source bug must not stop the run
            outcome = FetchOutcome(errors=[f"{type(e).__name__}: {str(e)[:200]}"])
        res = SourceResult(name, outcome.status, fetched=len(outcome.postings), errors=outcome.blocked + outcome.errors)
        for p in outcome.postings:
            self._ingest(p, res)
        for unit, ids in outcome.complete_ids.items():
            res.closed += self._close_missing(unit, ids)
        self._save_cursors(name, ctx.cursors)
        self._record_run(res, started)
        self._health(name)
        log.info("source done", extra={"data": res.__dict__})
        return res

    def _ingest(self, p: Posting, res: SourceResult) -> None:
        p.title = " ".join((p.title or "").split())
        p.company = " ".join((p.company or "").split())
        if not p.url or not p.title:
            return
        now = iso(self.clock.now())
        if self.conn.execute("SELECT 1 FROM jobs WHERE id = ?", (p.id,)).fetchone():
            with tx(self.conn):
                self.conn.execute("UPDATE jobs SET last_seen = ? WHERE id = ?", (now, p.id))
            return
        res.new += 1
        d = decide(p, self.cfg.filters, self.cfg.policy.work_authorized_countries)
        fp = T.fingerprint(p.company, p.title, p.location)
        data = {
            "description": T.clip(p.description, DESCRIPTION_CHARS),
            "remote_text": p.remote,
            "extra": p.extra,
            **d.signals,
        }
        create_entity(
            self.conn,
            self.clock,
            "job",
            p.id,
            actor="collect",
            columns={
                "source": p.source,
                "kind": p.kind,
                "company": p.company,
                "title": p.title,
                "url": p.url,
                "location": p.location,
                "country": p.country,
                "posted_at": p.posted_at,
                "fingerprint": fp,
                "last_seen": now,
            },
            data=data,
        )
        if not d.keep:
            self._drop(p.id, "discovered", d.rule or "prefilter", d.matched)
            res.dropped += 1
            return
        transition(self.conn, self.clock, "job", p.id, "prefiltered", actor="collect")
        if self._dedupe(p, fp):
            res.dropped += 1
        else:
            res.kept += 1

    def _drop(
        self, job_id: str, expected: str, rule: str, matched: str | None, duplicate_of: str | None = None
    ) -> None:
        transition(
            self.conn,
            self.clock,
            "job",
            job_id,
            "dropped",
            expected=expected,
            actor="collect",
            data={"rule": rule, "matched": matched, "duplicate_of": duplicate_of},
        )
        with tx(self.conn):
            self.conn.execute(
                "UPDATE jobs SET drop_rule = ?, duplicate_of = ? WHERE id = ?", (rule, duplicate_of, job_id)
            )

    def _dedupe(self, p: Posting, fp: str) -> bool:
        """Keep one copy per fingerprint; an ATS record beats an aggregator, which beats a community
        post. Returns True if the new posting was dropped as a duplicate."""
        if not T.norm_company(p.company):
            return False  # no company (e.g. a Telegram post): the fingerprint is too weak to merge on
        marks = ", ".join("?" for _ in LIVE_STATES)
        row = self.conn.execute(
            f"SELECT id, kind, state, digested_at FROM jobs WHERE fingerprint = ? AND id != ? AND state IN ({marks}) "
            "ORDER BY created_at LIMIT 1",
            (fp, p.id, *LIVE_STATES),
        ).fetchone()
        if row is None:
            return False
        new_better = KIND_RANK[p.kind] < KIND_RANK.get(row["kind"], 9)
        if new_better and row["state"] == "prefiltered" and row["digested_at"] is None:
            self._drop(row["id"], "prefiltered", "duplicate", None, duplicate_of=p.id)
            return False
        self._drop(p.id, "prefiltered", "duplicate", None, duplicate_of=row["id"])
        return True

    def _close_missing(self, unit: str, present: set[str]) -> int:
        """A complete board no longer lists these jobs: they closed."""
        prefix = unit + ":"
        terminal = states.terminal_states(states.JOB)
        rows = self.conn.execute("SELECT id, state FROM jobs WHERE id LIKE ? || '%'", (prefix,)).fetchall()
        n = 0
        for r in rows:
            if r["id"] not in present and r["state"] not in terminal:
                transition(self.conn, self.clock, "job", r["id"], "closed", expected=r["state"], actor="collect")
                n += 1
        return n

    # --- bookkeeping ---------------------------------------------------------------------

    def _cursors(self, source: str) -> dict[str, str]:
        rows = self.conn.execute("SELECT key, value FROM source_cursors WHERE source = ?", (source,)).fetchall()
        return {r["key"]: r["value"] for r in rows}

    def _save_cursors(self, source: str, cursors: dict[str, str]) -> None:
        with tx(self.conn):
            for k, v in cursors.items():
                self.conn.execute(
                    "INSERT INTO source_cursors(source, key, value) VALUES (?, ?, ?) "
                    "ON CONFLICT(source, key) DO UPDATE SET value = excluded.value",
                    (source, k, str(v)),
                )

    def _record_run(self, r: SourceResult, started: str) -> None:
        with tx(self.conn):
            self.conn.execute(
                "INSERT INTO source_runs(source, started_at, finished_at, status, fetched, new, kept, dropped, closed, "
                "error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    r.source,
                    started,
                    iso(self.clock.now()),
                    r.status,
                    r.fetched,
                    r.new,
                    r.kept,
                    r.dropped,
                    r.closed,
                    json.dumps(r.errors[:5], ensure_ascii=False) if r.errors else None,
                ),
            )

    def _health(self, source: str) -> None:
        """Alert once when a source reaches N consecutive non-ok runs, and once when it recovers."""
        n = self.cfg.sources.health_alert_after
        rows = self.conn.execute(
            "SELECT status, error FROM source_runs WHERE source = ? ORDER BY id DESC LIMIT ?", (source, n + 1)
        ).fetchall()
        bad = [r["status"] not in ("ok", "skipped") for r in rows]
        if len(bad) >= n and all(bad[:n]) and (len(bad) == n or not bad[n]):
            err = rows[0]["error"] or ""
            self.notifier.send(f"Источник {source}: {n} запуска подряд со статусом {rows[0]['status']}.\n{err[:500]}")
        elif len(bad) == n + 1 and not bad[0] and all(bad[1:]):
            self.notifier.send(f"Источник {source} снова работает.", silent=True)
