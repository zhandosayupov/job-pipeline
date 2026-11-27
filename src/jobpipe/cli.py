"""`jobpipe` command line. Timers call `jobpipe run ...`; the service calls `jobpipe bot`."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shutil
import sys
from pathlib import Path

from .adapters.notifier.telegram import LogNotifier, TelegramNotifier
from .adapters.storage.db import open_db
from .clock import SystemClock
from .config.settings import REPO_ROOT, AppConfig, load_config
from .logging_setup import setup_logging
from .ports.notifier import Notifier
from .services import control
from .services.queue import Queue

log = logging.getLogger("jobpipe")


def _notifier(cfg: AppConfig) -> Notifier:
    token = cfg.secrets.get(cfg.settings.telegram.token_env)
    owner = cfg.settings.telegram.owner_id
    if token and owner:
        return TelegramNotifier(token, owner)
    return LogNotifier()


def _router_factory(cfg: AppConfig, conn):
    def make():
        from .adapters.llm.openai_compat import OpenAICompatTransport
        from .services.llm_router import Router
        from .services.prompts import PromptStore

        return Router(conn, SystemClock(), cfg.llm, cfg.secrets, OpenAICompatTransport(),
                      PromptStore(cfg.paths.prompts_dir))

    return make


# --- commands ------------------------------------------------------------------------


def cmd_init(cfg: AppConfig, args) -> int:
    """Create JOBPIPE_HOME with example config. Never overwrites existing files."""
    home = cfg.paths.home
    for d in (cfg.paths.config_dir, cfg.paths.data_dir, home / "kb"):
        d.mkdir(parents=True, exist_ok=True)
    example = REPO_ROOT / "config.example"
    for src in sorted(example.glob("*.yaml")):
        dst = cfg.paths.config_dir / src.name
        if not dst.exists():
            shutil.copyfile(src, dst)
            print(f"created {dst}")
    secrets = home / "secrets.env"
    if not secrets.exists() and not Path("/etc/jobpipe/secrets.env").exists():
        shutil.copyfile(REPO_ROOT / "ops" / "secrets.env.example", secrets)
        if os.name != "nt":
            secrets.chmod(0o600)
        print(f"created {secrets} (fill it in; never commit it)")
    guard = home / "leakguard.txt"
    if not guard.exists():
        guard.write_text("# One private string per line (email, phone, full name). Blocks commits.\n",
                         encoding="utf-8")
        print(f"created {guard}")
    open_db(cfg.paths.db_path).close()
    print(f"database ready at {cfg.paths.db_path}")
    return 0


def cmd_migrate(cfg: AppConfig, args) -> int:
    from .adapters.storage.db import connect, migrate

    conn = connect(cfg.paths.db_path)
    applied = migrate(conn)
    print("applied: " + (", ".join(applied) if applied else "nothing, schema is current"))
    return 0


def cmd_bot(cfg: AppConfig, args) -> int:
    from .bot.app import run_bot

    open_db(cfg.paths.db_path).close()
    asyncio.run(run_bot(cfg))
    return 0


def cmd_run(cfg: AppConfig, args) -> int:
    conn = open_db(cfg.paths.db_path)
    if args.what == "queue":
        from .workers.handlers import HANDLERS
        from .workers.runner import Runner

        runner = Runner(conn, SystemClock(), cfg, HANDLERS, _notifier(cfg),
                        services={"router": _router_factory(cfg, conn)})
        types = args.types.split(",") if args.types else None
        summary = runner.run(max_seconds=args.max_seconds, types=types)
        log.info("queue run finished", extra={"data": summary.__dict__})
        return 0 if summary.status in {"ok", "paused", "stopped"} else 1
    if args.what == "retention":
        from .services.retention import run_retention

        counts = run_retention(conn, SystemClock(), cfg.settings.retention, dry_run=args.dry_run)
        print(("dry run, nothing changed: " if args.dry_run else "") +
              ", ".join(f"{k} {v}" for k, v in counts.items()))
        log.info("retention finished", extra={"data": {"dry_run": args.dry_run, **counts}})
        return 0
    if args.what == "collect":
        return _run_collect(cfg, conn, args)
    if args.what == "digest":
        from .services import digest

        if args.dry_run:
            d = digest.build(conn, SystemClock(), cfg.settings.timezone, cfg.sources.digest_max_items)
            print("\n\n---\n\n".join(d.messages))
            return 0
        sent = digest.send(conn, SystemClock(), _notifier(cfg), cfg.settings.timezone, cfg.sources.digest_max_items)
        if sent is None:
            print("digest not delivered; jobs stay pending for the next digest", file=sys.stderr)
            return 1
        print(f"digest sent: {len(sent.job_ids)} jobs in {len(sent.messages)} message(s)")
        return 0
    print(f"`run {args.what}` arrives in a later sprint", file=sys.stderr)
    return 2


def _run_collect(cfg: AppConfig, conn, args) -> int:
    """Fetch sources. --dry-run works on a temporary copy of the database and prints what it found."""
    import tempfile

    from .adapters.http import Http
    from .adapters.storage.db import connect, migrate
    from .services.backup import backup_db
    from .services.collect import Collector

    h = cfg.sources.http
    http = Http(user_agent=h.user_agent, min_interval_s=h.min_interval_s, timeout_s=h.timeout_s,
                respect_robots=h.respect_robots)
    only = args.sources.split(",") if args.sources else None
    notifier = _notifier(cfg)
    if args.dry_run:
        tmp = Path(tempfile.mkdtemp(prefix="jobpipe-dry-"))
        copy = backup_db(conn, tmp, SystemClock(), keep=1)
        conn = connect(copy)
        migrate(conn)
        from .adapters.notifier.telegram import RecordingNotifier

        notifier = RecordingNotifier()
    results = Collector(conn, SystemClock(), cfg, http, notifier).run(only)
    for r in results:
        err = f"  {r.errors[0][:100]}" if r.errors else ""
        print(f"{r.source:11} {r.status:8} fetched {r.fetched:4} new {r.new:4} kept {r.kept:4} "
              f"dropped {r.dropped:4} closed {r.closed:3}{err}")
    if args.dry_run:
        rows = conn.execute("SELECT drop_rule, COUNT(*) n FROM jobs WHERE drop_rule IS NOT NULL GROUP BY 1").fetchall()
        print("dropped by rule: " + ", ".join(f"{r['drop_rule']} {r['n']}" for r in rows))
        print("dry run: the real database was not changed")
    return 0


def cmd_jobs(cfg: AppConfig, args) -> int:
    """Inspect jobs: `jobs list --state prefiltered`, `jobs dropped` (audit the filters)."""
    conn = open_db(cfg.paths.db_path)
    if args.action == "dropped":
        rows = conn.execute(
            "SELECT id, title, company, drop_rule, data_json FROM jobs WHERE state = 'dropped' "
            "ORDER BY updated_at DESC LIMIT ?", (args.limit,)).fetchall()
        for r in rows:
            print(f"{r['drop_rule'] or '':16} {r['company'][:24]:24} {r['title'][:60]}")
        return 0
    q = "SELECT id, state, company, title, url, digested_at FROM jobs"
    params: tuple = ()
    if args.state:
        q += " WHERE state = ?"
        params = (args.state,)
    for r in conn.execute(q + " ORDER BY created_at DESC LIMIT ?", (*params, args.limit)).fetchall():
        mark = "*" if r["digested_at"] else " "
        print(f"{mark} {r['state']:11} {r['company'][:24]:24} {r['title'][:55]:55} {r['url']}")
    return 0


def cmd_effects(cfg: AppConfig, args) -> int:
    """List or resolve side effects whose outcome needs the owner's decision (ADR 0014)."""
    conn = open_db(cfg.paths.db_path)
    se = control.SideEffects(conn, SystemClock())
    if args.action == "resolve":
        try:
            se.resolve(args.key, args.outcome)
        except KeyError:
            print(f"no side effect with key {args.key!r}", file=sys.stderr)
            return 1
        print(f"{args.key}: {args.outcome}")
        return 0
    rows = conn.execute(
        "SELECT key, kind, status, updated_at FROM side_effects WHERE status = ? ORDER BY updated_at DESC LIMIT 50",
        (args.status,),
    ).fetchall()
    for r in rows:
        print(f"{r['key']:<50} {r['kind']:<12} {r['status']:<8} {r['updated_at']}")
    return 0


def cmd_task(cfg: AppConfig, args) -> int:
    conn = open_db(cfg.paths.db_path)
    q = Queue(conn, SystemClock(), cfg.settings.queue)
    if args.action == "add":
        payload = json.loads(args.payload) if args.payload else {}
        task_id, created = q.enqueue(args.type, payload, idempotency_key=args.key, max_attempts=args.max_attempts)
        print(f"task #{task_id} {'created' if created else 'already exists'}")
        return 0
    rows = conn.execute(
        "SELECT id, type, status, attempts, max_attempts, next_run_at, last_error FROM tasks "
        + ("WHERE status = ? " if args.status else "")
        + "ORDER BY id DESC LIMIT 30",
        (args.status,) if args.status else (),
    ).fetchall()
    for r in rows:
        err = f"  {r['last_error'][:60]}" if r["last_error"] else ""
        print(f"#{r['id']:<5} {r['type']:<18} {r['status']:<8} {r['attempts']}/{r['max_attempts']}  "
              f"next {r['next_run_at']}{err}")
    return 0


def cmd_pause(cfg: AppConfig, args) -> int:
    from .bot.app import BotService

    open_db(cfg.paths.db_path).close()
    print(BotService(cfg).pause())
    return 0


def cmd_resume(cfg: AppConfig, args) -> int:
    from .bot.app import BotService

    open_db(cfg.paths.db_path).close()
    print(BotService(cfg).resume())
    return 0


def cmd_status(cfg: AppConfig, args) -> int:
    from .bot.app import BotService

    open_db(cfg.paths.db_path).close()
    print(BotService(cfg).status_text())
    return 0


def cmd_llm(cfg: AppConfig, args) -> int:
    from .workers.handlers import PingOut

    conn = open_db(cfg.paths.db_path)
    router = _router_factory(cfg, conn)()
    try:
        result = router.complete("ping", {"word": args.word}, schema=PingOut)
    except Exception as e:
        print(f"failed: {e}", file=sys.stderr)
        return 1
    print(f"{result.provider} ({result.model}) answered in {result.latency_ms} ms: {result.data}")
    print("route: " + "; ".join(result.attempts))
    return 0


def cmd_backup(cfg: AppConfig, args) -> int:
    from .services.backup import backup_db, upload

    conn = open_db(cfg.paths.db_path)
    path = backup_db(conn, cfg.paths.data_dir / "backups", SystemClock(), keep=cfg.settings.backup.keep)
    print(f"backup written: {path}")
    if cfg.settings.backup.upload_cmd:
        if control.is_paused(conn):
            print("paused: upload skipped")
        else:
            upload(path, cfg.settings.backup.upload_cmd)
            print("uploaded")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="jobpipe")
    p.add_argument("--home", help="JOBPIPE_HOME (default: env or ~/.jobpipe)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="create JOBPIPE_HOME with example config").set_defaults(fn=cmd_init)
    sub.add_parser("migrate", help="apply database migrations").set_defaults(fn=cmd_migrate)
    sub.add_parser("bot", help="run the resident Telegram bot + /healthz").set_defaults(fn=cmd_bot)

    run = sub.add_parser("run", help="oneshot job, started by a timer")
    run.add_argument("what", choices=["queue", "retention", "collect", "label", "tailor", "inbox", "digest"])
    run.add_argument("--max-seconds", type=float, default=None)
    run.add_argument("--types", help="comma-separated task types to take")
    run.add_argument("--sources", help="collect: comma-separated source names (also runs disabled ones)")
    run.add_argument("--dry-run", action="store_true",
                     help="retention/collect: work on a copy, change nothing; digest: print instead of sending")
    run.set_defaults(fn=cmd_run)

    jobs = sub.add_parser("jobs", help="inspect collected jobs")
    jobs.add_argument("action", choices=["list", "dropped"])
    jobs.add_argument("--state")
    jobs.add_argument("--limit", type=int, default=30)
    jobs.set_defaults(fn=cmd_jobs)

    eff = sub.add_parser("effects", help="external actions whose outcome needs your decision")
    eff_sub = eff.add_subparsers(dest="action", required=True)
    el = eff_sub.add_parser("list")
    el.add_argument("--status", default="unknown", choices=["planned", "done", "failed", "unknown"])
    er = eff_sub.add_parser("resolve")
    er.add_argument("key")
    er.add_argument("outcome", choices=["done", "failed", "reset"],
                    help="done: it happened · failed: it did not, keep blocked · reset: allow it again")
    eff.set_defaults(fn=cmd_effects)

    task = sub.add_parser("task", help="queue tools")
    task_sub = task.add_subparsers(dest="action", required=True)
    add = task_sub.add_parser("add")
    add.add_argument("type")
    add.add_argument("--payload", help="JSON object")
    add.add_argument("--key", help="idempotency key")
    add.add_argument("--max-attempts", type=int)
    lst = task_sub.add_parser("list")
    lst.add_argument("--status", choices=["pending", "running", "done", "dead"])
    task.set_defaults(fn=cmd_task)

    sub.add_parser("pause", help="kill switch on").set_defaults(fn=cmd_pause)
    sub.add_parser("resume", help="kill switch off").set_defaults(fn=cmd_resume)
    sub.add_parser("status", help="print /status").set_defaults(fn=cmd_status)

    llm = sub.add_parser("llm", help="LLM tools")
    llm.add_argument("action", choices=["ping"])
    llm.add_argument("--word", default="pong")
    llm.set_defaults(fn=cmd_llm)

    sub.add_parser("backup", help="online SQLite backup (+ optional upload)").set_defaults(fn=cmd_backup)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config(Path(args.home) if args.home else None)
    setup_logging(cfg.settings.log_level, cfg.secrets.values())
    return args.fn(cfg, args)


if __name__ == "__main__":
    raise SystemExit(main())
