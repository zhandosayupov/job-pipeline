"""SQLite connection, transactions and migrations (ADR 0002)."""
from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources
from pathlib import Path

from ...clock import Clock, SystemClock, iso

_MIGRATION_RE = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


def connect(path: Path | str) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=5.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # Must precede the first CREATE TABLE to take effect; retention then reclaims space with
    # `PRAGMA incremental_vacuum` instead of a full VACUUM (which needs 2x disk).
    conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE ... COMMIT; rollback on any exception. Not reentrant."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def available_migrations() -> list[tuple[int, str, str]]:
    pkg = resources.files("jobpipe.adapters.storage") / "migrations"
    out = []
    for entry in pkg.iterdir():
        m = _MIGRATION_RE.match(entry.name)
        if m:
            out.append((int(m.group(1)), entry.name, entry.read_text(encoding="utf-8")))
    out.sort()
    versions = [v for v, _, _ in out]
    if versions != list(range(1, len(versions) + 1)):
        raise RuntimeError(f"migration numbers must be 1..N without gaps, got {versions}")
    return out


def current_version(conn: sqlite3.Connection) -> int:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, "
        "applied_at TEXT NOT NULL)"
    )
    row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    return row[0] or 0


def migrate(conn: sqlite3.Connection, clock: Clock | None = None) -> list[str]:
    """Apply pending migrations, each in its own transaction. Returns the names applied."""
    clock = clock or SystemClock()
    applied = []
    have = current_version(conn)
    for version, name, sql in available_migrations():
        if version <= have:
            continue
        with tx(conn):
            for stmt in _split_sql(sql):
                conn.execute(stmt)
            conn.execute(
                "INSERT INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?)",
                (version, name, iso(clock.now())),
            )
        applied.append(name)
    return applied


def _split_sql(sql: str) -> list[str]:
    """Split a migration into complete statements using SQLite's own tokenizer, so ';' inside
    string literals and CREATE TRIGGER ... BEGIN ... END; bodies are handled correctly."""
    statements, buf = [], ""
    for line in sql.splitlines(keepends=True):
        if not buf and line.strip().startswith("--"):
            continue
        buf += line
        if sqlite3.complete_statement(buf):
            if buf.strip().strip(";").strip():
                statements.append(buf.strip())
            buf = ""
    leftover = "\n".join(ln for ln in buf.splitlines() if not ln.strip().startswith("--")).strip()
    if leftover:
        raise ValueError(f"incomplete SQL statement at end of migration: {leftover[:80]!r}")
    return statements


def open_db(path: Path | str, clock: Clock | None = None) -> sqlite3.Connection:
    conn = connect(path)
    migrate(conn, clock)
    return conn
