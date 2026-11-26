"""Block secrets and private data from entering the public repo (ADR 0005).

Usage:
  python tools/leak_guard.py --staged     # pre-commit hook: staged content
  python tools/leak_guard.py --all        # CI: every tracked file
  python tools/leak_guard.py FILE...      # explicit files

Private strings (your email, phone, full name) go one per line in $JOBPIPE_HOME/leakguard.txt,
which is itself private. A line containing `leak-guard: allow` is skipped.
"""
from __future__ import annotations

import argparse
import fnmatch
import os
import re
import subprocess
import sys
from pathlib import Path

PATTERNS = {
    "telegram bot token": re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b"),
    "google api key": re.compile(r"AIza[0-9A-Za-z_\-]{35}"),
    "sk- style api key": re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}"),
    "groq api key": re.compile(r"\bgsk_[A-Za-z0-9]{20,}"),
    "cerebras api key": re.compile(r"\bcsk-[A-Za-z0-9]{20,}"),
    "private key block": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    "telethon string session": re.compile(r"\b1[A-Za-z0-9_\-+/=]{300,}"),
    "google app password": re.compile(r"(?i)app[_ ]?password\s*[:=]\s*['\"]?[a-z]{4} ?[a-z]{4} ?[a-z]{4} ?[a-z]{4}\b"),
}

FORBIDDEN_PATHS = [
    "secrets.env", "*/secrets.env", "*.db", "*.db-wal", "*.db-shm",
    "kb/*", "data/*", "profiles/*", "config/*", "home/*", "leakguard.txt",
]
ALLOW_MARK = "leak-guard: allow"
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".pdf", ".ico", ".woff", ".woff2"}


def private_strings() -> list[str]:
    home = Path(os.environ.get("JOBPIPE_HOME") or Path.home() / ".jobpipe").expanduser()
    f = home / "leakguard.txt"
    if not f.exists():
        return []
    out = []
    for line in f.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s and not s.startswith("#") and len(s) >= 4:
            out.append(s)
    return out


def scan_text(path: str, text: str, private: list[str]) -> list[str]:
    problems = []
    lowered_private = [(p, p.lower()) for p in private]
    for n, line in enumerate(text.splitlines(), 1):
        if ALLOW_MARK in line:
            continue
        for name, rx in PATTERNS.items():
            if rx.search(line):
                problems.append(f"{path}:{n}: looks like a {name}")
        low = line.lower()
        for _, p in lowered_private:
            if p in low:
                problems.append(f"{path}:{n}: contains a private string from leakguard.txt")
    return problems


def scan_path_name(path: str) -> list[str]:
    norm = path.replace("\\", "/")
    for pat in FORBIDDEN_PATHS:
        if fnmatch.fnmatch(norm, pat):
            return [f"{path}: private path pattern {pat!r} must not be committed"]
    return []


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True,
                          encoding="utf-8").stdout


def collect(mode: str, files: list[str]) -> list[tuple[str, str | None]]:
    if mode == "staged":
        names = [n for n in _git("diff", "--cached", "--name-only", "--diff-filter=ACMR").splitlines() if n]
        out = []
        for n in names:
            if Path(n).suffix.lower() in SKIP_SUFFIXES:
                out.append((n, None))
                continue
            try:
                out.append((n, _git("show", f":{n}")))
            except (subprocess.CalledProcessError, UnicodeDecodeError):
                out.append((n, None))
        return out
    if mode == "all":
        files = [n for n in _git("ls-files").splitlines() if n]
    out = []
    for n in files:
        p = Path(n)
        if p.suffix.lower() in SKIP_SUFFIXES or not p.is_file():
            out.append((n, None))
            continue
        try:
            out.append((n, p.read_text(encoding="utf-8")))
        except UnicodeDecodeError:
            out.append((n, None))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--staged", action="store_true")
    g.add_argument("--all", action="store_true")
    ap.add_argument("files", nargs="*")
    args = ap.parse_args(argv)
    mode = "staged" if args.staged else "all" if args.all else "files"
    private = private_strings()
    problems: list[str] = []
    for name, text in collect(mode, args.files):
        problems += scan_path_name(name)
        if text is not None:
            problems += scan_text(name, text, private)
    if problems:
        print("leak guard: blocked\n  " + "\n  ".join(problems), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
