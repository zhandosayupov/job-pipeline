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


def verify_restore(backup: Path, work_dir: Path) -> dict:
    """Restore test: copy the backup with the backup API into a fresh file, run integrity_check and
    the migrations on the copy, and count the main tables. Proves the file can really be restored."""
    from ..adapters.storage.db import available_migrations, connect, current_version, migrate

    work_dir.mkdir(parents=True, exist_ok=True)
    target = work_dir / "restore-test.db"
    for suffix in ("", "-wal", "-shm"):
        Path(f"{target}{suffix}").unlink(missing_ok=True)
    src = sqlite3.connect(f"file:{backup}?mode=ro", uri=True)
    dst = sqlite3.connect(str(target))
    try:
        src.backup(dst)
    finally:
        src.close()
        dst.close()
    conn = connect(target)
    try:
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError(f"{backup.name}: integrity check failed after restore")
        migrate(conn)
        counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in ("jobs", "applications", "conversations", "events", "side_effects")}
        counts["schema"] = current_version(conn)
        if counts["schema"] != len(available_migrations()):
            raise RuntimeError("migrations did not bring the restored copy up to date")
        return counts
    finally:
        conn.close()
        for suffix in ("", "-wal", "-shm"):
            Path(f"{target}{suffix}").unlink(missing_ok=True)
