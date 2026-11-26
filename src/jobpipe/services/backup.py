"""Nightly SQLite backup with the online backup API (never a raw file copy of a live DB)."""
from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

from ..clock import Clock


def backup_db(conn: sqlite3.Connection, dest_dir: Path, clock: Clock, keep: int = 7) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / f"jobpipe-{clock.now():%Y%m%d-%H%M%S}.db"
    target = sqlite3.connect(str(path))
    try:
        conn.backup(target)
    finally:
        target.close()
    check = sqlite3.connect(str(path))
    try:
        if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError(f"backup {path} failed integrity check")
    finally:
        check.close()
    old = sorted(dest_dir.glob("jobpipe-*.db"))[:-keep] if keep > 0 else []
    for p in old:
        p.unlink()
    return path


def upload(path: Path, cmd: list[str]) -> None:
    """Run the configured upload command (e.g. an age-encrypt + gcloud storage cp script)."""
    subprocess.run([part.replace("{path}", str(path)) for part in cmd], check=True, timeout=600)
