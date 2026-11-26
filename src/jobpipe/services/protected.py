"""Protected pool (spec §11): companies that never get automatic actions.

A job matches when its company (normalised) equals an entry's name or alias, its URL host is one of
the entry's domains (or a subdomain), or its board token (Greenhouse/Ashby/Lever) is one of the
entry's ATS tokens. Entries past their `until` date are ignored. `/protect` and `/unprotect` edit
protected.yaml through this module and log an event.
"""
from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from ..clock import Clock
from ..config.settings import ProtectedCfg, ProtectedEntry
from .events import record_event
from .text import norm_company


class ProtectedPool:
    def __init__(self, cfg: ProtectedCfg, today: date | None = None):
        today = today or date.today()
        self.entries = [e for e in cfg.companies if not e.until or date.fromisoformat(e.until) >= today]
        self._names: dict[str, ProtectedEntry] = {}
        self._tokens: dict[str, ProtectedEntry] = {}
        for e in self.entries:
            for n in [e.name, *e.aliases]:
                if norm_company(n):
                    self._names[norm_company(n)] = e
            for t in e.ats_tokens:
                self._tokens[t.lower()] = e

    def match(self, company: str | None, url: str | None = None, source: str | None = None,
              source_job_id: str | None = None) -> ProtectedEntry | None:
        e = self._names.get(norm_company(company))
        if e:
            return e
        if source in {"greenhouse", "ashby", "lever"} and source_job_id and ":" in source_job_id:
            e = self._tokens.get(source_job_id.split(":", 1)[0].lower())
            if e:
                return e
        host = (urlsplit(url).hostname or "").lower() if url else ""
        if host:
            for entry in self.entries:
                for d in entry.domains:
                    d = d.lower().lstrip(".")
                    if host == d or host.endswith("." + d):
                        return entry
        return None


def load_file(path: Path) -> ProtectedCfg:
    if not path.exists():
        return ProtectedCfg()
    return ProtectedCfg.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})


def save_file(path: Path, cfg: ProtectedCfg) -> None:
    data = {"companies": [e.model_dump(exclude_defaults=True) | {"name": e.name} for e in cfg.companies]}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    tmp.replace(path)


def protect(path: Path, conn: sqlite3.Connection, clock: Clock, name: str, reason: str = "manual",
            actor: str = "owner") -> bool:
    """Add a company (and mark its live jobs protected). Returns False if it was already there."""
    cfg = load_file(path)
    if any(norm_company(e.name) == norm_company(name) or norm_company(name) in map(norm_company, e.aliases)
           for e in cfg.companies):
        return False
    cfg.companies.append(ProtectedEntry(name=name.strip(), reason=reason))
    save_file(path, cfg)
    mark_jobs(conn, ProtectedPool(cfg))
    record_event(conn, clock, entity="protected", entity_id=norm_company(name), kind="added", actor=actor,
                 data={"name": name, "reason": reason})
    return True


def unprotect(path: Path, conn: sqlite3.Connection, clock: Clock, name: str, actor: str = "owner") -> bool:
    cfg = load_file(path)
    key = norm_company(name)
    kept = [e for e in cfg.companies
            if norm_company(e.name) != key and key not in map(norm_company, e.aliases)]
    if len(kept) == len(cfg.companies):
        return False
    save_file(path, ProtectedCfg(companies=kept))
    pool = ProtectedPool(ProtectedCfg(companies=kept))
    rows = conn.execute("SELECT id, company, url, source FROM jobs WHERE protected = 1").fetchall()
    for r in rows:
        if not pool.match(r["company"], r["url"], r["source"], r["id"].split(":", 1)[-1]):
            conn.execute("UPDATE jobs SET protected = 0 WHERE id = ?", (r["id"],))
    record_event(conn, clock, entity="protected", entity_id=key, kind="removed", actor=actor, data={"name": name})
    return True


def mark_jobs(conn: sqlite3.Connection, pool: ProtectedPool) -> int:
    """Set jobs.protected for every live job that matches the pool. Returns how many changed."""
    rows = conn.execute(
        "SELECT id, company, url, source FROM jobs WHERE protected = 0 AND state NOT IN ('dropped', 'closed')"
    ).fetchall()
    n = 0
    for r in rows:
        if pool.match(r["company"], r["url"], r["source"], r["id"].split(":", 1)[-1]):
            conn.execute("UPDATE jobs SET protected = 1 WHERE id = ?", (r["id"],))
            n += 1
    return n
