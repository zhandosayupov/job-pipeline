from __future__ import annotations

import random
from datetime import UTC, datetime
from pathlib import Path

import pytest

from jobpipe.adapters.storage.db import open_db
from jobpipe.clock import FakeClock
from jobpipe.config.settings import (
    AppConfig,
    LLMConfig,
    Paths,
    QueueCfg,
    RunnerCfg,
    Secrets,
    Settings,
    TaskTypeCfg,
)

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(datetime(2026, 11, 26, 6, 0, tzinfo=UTC))


@pytest.fixture
def conn(tmp_path, clock):
    c = open_db(tmp_path / "t.db", clock)
    yield c
    c.close()


@pytest.fixture
def rng() -> random.Random:
    return random.Random(42)


@pytest.fixture
def queue_cfg() -> QueueCfg:
    return QueueCfg(
        lease_seconds=60,
        default=TaskTypeCfg(max_attempts=3, backoff_base_s=10, backoff_cap_s=100),
    )


@pytest.fixture
def app_cfg(tmp_path, queue_cfg) -> AppConfig:
    home = tmp_path / "home"
    paths = Paths(
        home=home,
        config_dir=home / "config",
        data_dir=home / "data",
        db_path=home / "data" / "jobpipe.db",
        prompts_dir=REPO / "prompts",
        secrets_file=home / "secrets.env",
    )
    settings = Settings(queue=queue_cfg, runner=RunnerCfg(max_seconds=20, hard_timeout_s=30))
    return AppConfig(paths=paths, settings=settings, llm=LLMConfig(), secrets=Secrets({}))
