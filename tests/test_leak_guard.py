import importlib.util

from .conftest import REPO

spec = importlib.util.spec_from_file_location("leak_guard", REPO / "tools" / "leak_guard.py")
lg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lg)

# Fake secrets are assembled at runtime so this file itself passes the guard.
FAKE_TG = "1234567890" + ":" + "A" * 35
FAKE_GOOGLE = "AI" + "za" + "B" * 35
FAKE_SK = "sk" + "-" + "c" * 30


def test_detects_secret_shapes():
    for secret in (FAKE_TG, FAKE_GOOGLE, FAKE_SK, "-----BEGIN " + "PRIVATE KEY-----"):
        assert lg.scan_text("f.py", f"x = '{secret}'", []), secret


def test_allow_marker_and_clean_text():
    assert lg.scan_text("f.py", f"x = '{FAKE_SK}'  # leak-guard: allow", []) == []
    assert lg.scan_text("f.py", "chat_id = 123456789", []) == []


def test_private_strings_case_insensitive():
    assert lg.scan_text("f.md", "contact: Someone@Example.org", ["someone@example.org"])


def test_forbidden_paths():
    assert lg.scan_path_name("secrets.env")
    assert lg.scan_path_name("ops/secrets.env")
    assert lg.scan_path_name("kb/facts.yaml")
    assert lg.scan_path_name("data/jobpipe.db")
    assert not lg.scan_path_name("ops/secrets.env.example")
    assert not lg.scan_path_name("config.example/llm.yaml")
    assert not lg.scan_path_name("src/jobpipe/config/settings.py")
