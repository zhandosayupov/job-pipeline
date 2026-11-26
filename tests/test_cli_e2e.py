"""End-to-end through the real CLI entry point, offline: init → enqueue → run → status."""
from jobpipe.cli import main


def test_cli_end_to_end(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("JOBPIPE_SECRETS", str(tmp_path / "none.env"))
    home = str(tmp_path / "home")
    assert main(["--home", home, "init"]) == 0
    assert (tmp_path / "home" / "config" / "llm.yaml").exists()
    assert main(["--home", home, "task", "add", "selftest.ok", "--key", "demo"]) == 0
    assert main(["--home", home, "task", "add", "selftest.ok", "--key", "demo"]) == 0
    out = capsys.readouterr().out
    assert "created" in out and "already exists" in out
    assert main(["--home", home, "run", "queue", "--max-seconds", "5"]) == 0
    assert main(["--home", home, "task", "list"]) == 0
    assert "done" in capsys.readouterr().out
    assert main(["--home", home, "pause"]) == 0
    assert main(["--home", home, "run", "queue", "--max-seconds", "5"]) == 0  # paused: exits cleanly
    assert main(["--home", home, "status"]) == 0
    assert "ВКЛЮЧЕНА" in capsys.readouterr().out
    assert main(["--home", home, "backup"]) == 0
    assert list((tmp_path / "home" / "data" / "backups").glob("*.db"))


def test_cli_llm_ping_without_keys_fails_cleanly(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("JOBPIPE_SECRETS", str(tmp_path / "none.env"))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    home = str(tmp_path / "home")
    main(["--home", home, "init"])
    assert main(["--home", home, "llm", "ping"]) == 1
    assert "no key GEMINI_API_KEY" in capsys.readouterr().err


def test_cli_digest_dry_run_and_jobs(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("JOBPIPE_SECRETS", str(tmp_path / "none.env"))
    home = str(tmp_path / "home")
    main(["--home", home, "init"])
    assert (tmp_path / "home" / "config" / "sources.yaml").exists()
    assert main(["--home", home, "run", "digest", "--dry-run"]) == 0
    assert "Новых вакансий нет" in capsys.readouterr().out
    assert main(["--home", home, "jobs", "list"]) == 0
    assert main(["--home", home, "jobs", "dropped"]) == 0
