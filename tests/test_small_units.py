"""Config, logging redaction, PII, prompts, status rendering, backup."""
import json
import logging
import sqlite3
from datetime import UTC, datetime

import pytest

from jobpipe.bot.status import StatusSnapshot, render
from jobpipe.config.settings import load_config, parse_env_file
from jobpipe.logging_setup import JsonFormatter, Redactor
from jobpipe.services.backup import backup_db
from jobpipe.services.pii import Pseudonymizer
from jobpipe.services.prompts import PromptError, PromptStore, fill

from .conftest import REPO

# --- config ---------------------------------------------------------------------------


def test_parse_env_file():
    text = "# c\nA=1\nexport B='two'\nC=\"3\"\nbad line\nEMPTY=\n"
    assert parse_env_file(text) == {"A": "1", "B": "two", "C": "3", "EMPTY": ""}


def test_load_config_defaults_and_secrets(tmp_path, monkeypatch):
    monkeypatch.delenv("JOBPIPE_SECRETS", raising=False)
    home = tmp_path / "h"
    (home / "config").mkdir(parents=True)
    (home / "config" / "settings.yaml").write_text("telegram:\n  owner_id: 7\n", encoding="utf-8")
    (home / "secrets.env").write_text("TELEGRAM_BOT_TOKEN=from-file\n", encoding="utf-8")
    cfg = load_config(home, env={})
    assert cfg.settings.telegram.owner_id == 7
    assert cfg.settings.timezone == "Asia/Almaty"
    assert cfg.secrets.get("TELEGRAM_BOT_TOKEN") == "from-file"
    assert "from-file" not in repr(cfg.secrets)
    cfg2 = load_config(home, env={"TELEGRAM_BOT_TOKEN": "from-env"})
    assert cfg2.secrets.get("TELEGRAM_BOT_TOKEN") == "from-env"


def test_unknown_config_key_rejected(tmp_path):
    home = tmp_path / "h"
    (home / "config").mkdir(parents=True)
    (home / "config" / "settings.yaml").write_text("telegramm: {}\n", encoding="utf-8")
    with pytest.raises(Exception, match="telegramm"):
        load_config(home, env={})


def test_example_config_is_valid(tmp_path):
    home = tmp_path / "h"
    (home / "config").mkdir(parents=True)
    for f in (REPO / "config.example").glob("*.yaml"):
        (home / "config" / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
    cfg = load_config(home, env={})
    assert "gemini" in cfg.llm.providers
    assert cfg.llm.tasks["ping"].chain[0] == "gemini"


# --- logging --------------------------------------------------------------------------


def test_redactor_masks_secrets_emails_phones_but_not_ids():
    r = Redactor(["supersecretvalue"])
    text = ("token supersecretvalue mail a.b@corp.kz phone +7 701 234 56 78 local 8 (701) 234-56-78 "
            "chat_id 123456789 task 42")
    out = r(text)
    assert "supersecretvalue" not in out and "a.b@corp.kz" not in out
    assert "+7 701" not in out and "(701)" not in out
    assert "123456789" in out and "task 42" in out


def test_json_formatter_redacts_extra_data():
    rec = logging.LogRecord("x", logging.INFO, __file__, 1, "sent to %s", ("bob@x.com",), None)
    rec.data = {"key": "supersecretvalue"}
    line = JsonFormatter(Redactor(["supersecretvalue"])).format(rec)
    payload = json.loads(line)
    assert payload["msg"] == "sent to [email]" and payload["data"]["key"] == "[secret]"


# --- pii and prompts ------------------------------------------------------------------


def test_pseudonymizer_roundtrip_case_insensitive():
    p = Pseudonymizer({"NAME": ["Jane Doe", "Jane"], "EMAIL": ["jane@x.io"], "EMPTY": []})
    red = p.redact("JANE DOE (jane@x.io), aka Jane")
    assert red == "{{NAME}} ({{EMAIL}}), aka {{NAME}}"
    assert p.restore(red) == "Jane Doe (jane@x.io), aka Jane Doe"
    assert not Pseudonymizer({})


def test_prompt_store_and_fill(tmp_path):
    (tmp_path / "t").mkdir()
    (tmp_path / "t" / "v2.md").write_text("## system\nS {{a}}\n\n## user\nU {{ b }}\n", encoding="utf-8")
    prompt, msgs = PromptStore(tmp_path).render("t", "v2", {"a": 1, "b": "x"})
    assert prompt.version == "v2"
    assert msgs == [{"role": "system", "content": "S 1"}, {"role": "user", "content": "U x"}]
    with pytest.raises(PromptError, match="missing"):
        fill("{{a}} {{b}}", {"a": 1})
    with pytest.raises(PromptError, match="not found"):
        PromptStore(tmp_path).load("t", "v9")


def test_repo_ping_prompt_renders():
    _, msgs = PromptStore(REPO / "prompts").render("ping", "v1", {"word": "zz"})
    assert '"echo": "zz"' in msgs[-1]["content"]


# --- status ---------------------------------------------------------------------------


def test_status_render():
    snap = StatusSnapshot(
        version="0.1.0",
        now=datetime(2026, 11, 26, 6, 0, tzinfo=UTC),
        tz="Asia/Almaty",
        paused=True,
        schema_version=1,
        tasks={"dead": 1, "pending": 2},
        due=1,
        dead=[(7, "selftest.fail", "RuntimeError: boom")],
        running=[("run queue", 123, "2026-11-26T05:59:00.000000Z")],
        last_runs=[("run queue", "ok", "2026-11-26T05:58:00.000000Z")],
        llm_today=[{"provider": "gemini", "calls": 3, "ok": 2, "tokens": 120}],
        providers={"gemini": "cooldown until 06:01 UTC"},
        db_size_kb=64,
    )
    out = render(snap)
    assert "26.11 11:00 (Asia/Almaty)" in out
    assert "ВКЛЮЧЕНА" in out
    assert "dead 1, pending 2" in out and "к запуску сейчас 1" in out
    assert "#7 selftest.fail" in out
    assert "(pid 123) с 26.11 10:59" in out
    assert "gemini: 3 вызовов, ok 2, 120 токенов" in out
    assert "схема v1, 64 КБ" in out


# --- backup ---------------------------------------------------------------------------


def test_backup_keeps_n_and_is_consistent(conn, clock, tmp_path):
    conn.execute("INSERT INTO control(key, value, updated_at) VALUES ('paused', '0', 'x')")
    paths = []
    for _ in range(4):
        paths.append(backup_db(conn, tmp_path / "b", clock, keep=2))
        clock.advance(60)
    left = sorted((tmp_path / "b").glob("*.db"))
    assert left == paths[-2:]
    copy = sqlite3.connect(left[-1])
    assert copy.execute("SELECT value FROM control").fetchone()[0] == "0"
    copy.close()
