"""Configuration: YAML files in $JOBPIPE_HOME/config plus secrets.env.

Every file is optional; a missing file means defaults. Secrets never appear in reprs or logs.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

REPO_ROOT = Path(__file__).resolve().parents[3]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- settings.yaml -------------------------------------------------------------------


class TelegramCfg(_Model):
    owner_id: int | None = None
    token_env: str = "TELEGRAM_BOT_TOKEN"


class TaskTypeCfg(_Model):
    max_attempts: int = 5
    backoff_base_s: float = 30
    backoff_cap_s: float = 3600


class QueueCfg(_Model):
    # Must exceed runner.hard_timeout_s, or a task still running legitimately could be reclaimed.
    lease_seconds: int = 600
    max_reclaims: int = 3
    default: TaskTypeCfg = TaskTypeCfg()
    types: dict[str, TaskTypeCfg] = Field(default_factory=dict)

    def for_type(self, task_type: str) -> TaskTypeCfg:
        return self.types.get(task_type, self.default)


class RunnerCfg(_Model):
    max_seconds: int = 240        # stop taking new tasks after this
    hard_timeout_s: int = 300     # must equal TimeoutStartSec in ops/systemd/jobpipe-queue.service
    kill_grace_s: float = 8.0


class RetentionCfg(_Model):
    """ADR 0018. Days after which old rows are deleted or compacted to their resolution."""

    tasks_done_days: int = 7
    tasks_dead_days: int = 30
    runs_days: int = 14
    llm_calls_days: int = 35          # rolled up into llm_daily before deletion
    events_days: int = 60             # application/conversation events are kept: they are the funnel
    jobs_compact_days: int = 30       # finished jobs keep only `keep_fields`
    applications_compact_days: int = 180
    side_effects_compact_days: int = 90   # keys are kept forever (idempotency); payload dropped
    keep_fields: dict[str, list[str]] = Field(default_factory=lambda: {
        "job": ["company", "title", "url", "fingerprint", "source"],
        "application": ["company", "title", "url", "platform"],
    })


class BackupCfg(_Model):
    keep: int = 7
    # Optional command run after a backup, with {path} replaced, e.g. an age + gcloud upload script.
    upload_cmd: list[str] | None = None


class HttpCfg(_Model):
    host: str = "127.0.0.1"
    port: int = 8080


class Settings(_Model):
    timezone: str = "Asia/Almaty"
    telegram: TelegramCfg = TelegramCfg()
    queue: QueueCfg = QueueCfg()
    runner: RunnerCfg = RunnerCfg()
    backup: BackupCfg = BackupCfg()
    retention: RetentionCfg = RetentionCfg()
    http: HttpCfg = HttpCfg()
    log_level: str = "INFO"

    @model_validator(mode="after")
    def _lease_outlives_runner(self) -> Settings:
        if self.queue.lease_seconds <= self.runner.hard_timeout_s:
            raise ValueError(
                f"queue.lease_seconds ({self.queue.lease_seconds}) must exceed runner.hard_timeout_s "
                f"({self.runner.hard_timeout_s}), or running tasks get reclaimed"
            )
        if self.runner.max_seconds >= self.runner.hard_timeout_s:
            raise ValueError("runner.max_seconds must be below runner.hard_timeout_s")
        return self


# --- policy.yaml ---------------------------------------------------------------------

Level = Literal["L0", "L1", "L2"]
AnswerMode = Literal["ask", "preapproved", "posting_range", "work_authorization"]


class PolicyCfg(_Model):
    """Autonomy per action (ADR 0016). L0 notify, L1 draft + owner Approve, L2 act + report."""

    autonomy: dict[str, Level] = Field(default_factory=lambda: {
        "apply": "L2",
        "reply": "L1",
        "email_draft": "L1",
    })
    # Applications per platform per day; reaching it defers the rest to tomorrow.
    daily_caps: dict[str, int] = Field(default_factory=lambda: {"hh": 20, "greenhouse": 10, "lever": 10,
                                                                 "ashby": 10})
    # Messages touching these never go out without Approve, whatever the configured level.
    sensitive_topics: list[str] = Field(default_factory=lambda: [
        "compensation", "start_date", "notice_period", "visa", "citizenship", "reason_for_leaving",
        "references", "interview_scheduling", "offer",
    ])
    # How an application form answers each sensitive topic. Modes:
    #   ask                 always an approval card
    #   preapproved         verbatim entry from kb/answers.yaml, else a card
    #   posting_range       derive from the salary range the employer published, else a card
    #   work_authorization  derived from work_authorized_countries and the job's country
    #                       (sponsorship asked everywhere else), unknown country = card
    sensitive_answers: dict[str, AnswerMode] = Field(default_factory=lambda: {
        "compensation": "ask",
        "visa": "work_authorization",
        "citizenship": "work_authorization",
        "default": "preapproved",
    })
    # Where the owner may work without sponsorship: Kazakhstan, and the EAEU states, whose
    # citizens need no work permit in each other's countries.
    work_authorized_countries: list[str] = Field(default_factory=lambda: ["KZ", "AM", "KG", "BY", "RU"])
    # Messages (not applications) may be raised to L2 only after this many clean approvals.
    l2_min_clean_approvals: int = 20


# --- llm.yaml ------------------------------------------------------------------------


class LLMProvider(_Model):
    kind: Literal["openai_compat"] = "openai_compat"
    base_url: str
    model: str
    key_env: str
    enabled: bool = True
    rpm: int | None = None
    rpd: int | None = None
    tpm: int | None = None
    tpd: int | None = None
    day_reset_tz: str = "UTC"
    trains_on_data: bool = False
    timeout_s: float = 60
    json_mode: bool = True


class LLMTask(_Model):
    chain: list[str]
    prompt_version: str = "v1"
    temperature: float = 0.2
    max_tokens: int = 1024


class LLMConfig(_Model):
    providers: dict[str, LLMProvider] = Field(default_factory=dict)
    tasks: dict[str, LLMTask] = Field(default_factory=dict)
    pii_redaction: Literal["required", "off"] = "required"
    personal_on_training_providers: bool = False
    breaker_threshold: int = 3
    breaker_minutes: float = 15
    default_cooldown_s: float = 60
    retry_backoff_s: float = 2.0
    # Personal values replaced by placeholders before LLM calls marked personal (ADR 0008).
    pii_values: dict[str, list[str]] = Field(default_factory=dict)


# --- secrets -------------------------------------------------------------------------


class Secrets:
    """Key/value secrets. Never printed: repr shows only the key names."""

    def __init__(self, values: dict[str, str]):
        self._values = {k: v for k, v in values.items() if v}

    def get(self, key: str) -> str | None:
        return self._values.get(key)

    def values(self) -> list[str]:
        return list(self._values.values())

    def __contains__(self, key: str) -> bool:
        return key in self._values

    def __repr__(self) -> str:
        return f"Secrets(keys={sorted(self._values)})"


def parse_env_file(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:]
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        out[key.strip()] = value
    return out


# --- paths and loading ---------------------------------------------------------------


@dataclass(frozen=True)
class Paths:
    home: Path
    config_dir: Path
    data_dir: Path
    db_path: Path
    prompts_dir: Path
    secrets_file: Path


@dataclass
class AppConfig:
    paths: Paths
    settings: Settings
    llm: LLMConfig
    secrets: Secrets
    policy: PolicyCfg = field(default_factory=PolicyCfg)


def resolve_paths(home: Path | None = None) -> Paths:
    home = Path(home or os.environ.get("JOBPIPE_HOME") or Path.home() / ".jobpipe").expanduser()
    secrets = os.environ.get("JOBPIPE_SECRETS")
    if secrets:
        secrets_file = Path(secrets)
    elif Path("/etc/jobpipe/secrets.env").exists():
        secrets_file = Path("/etc/jobpipe/secrets.env")
    else:
        secrets_file = home / "secrets.env"
    prompts = Path(os.environ.get("JOBPIPE_PROMPTS") or REPO_ROOT / "prompts")
    data = home / "data"
    return Paths(
        home=home,
        config_dir=home / "config",
        data_dir=data,
        db_path=data / "jobpipe.db",
        prompts_dir=prompts,
        secrets_file=secrets_file,
    )


def _read_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: top level must be a mapping")
    return data


def load_config(home: Path | None = None, env: dict[str, str] | None = None) -> AppConfig:
    paths = resolve_paths(home)
    settings = Settings.model_validate(_read_yaml(paths.config_dir / "settings.yaml"))
    llm = LLMConfig.model_validate(_read_yaml(paths.config_dir / "llm.yaml"))
    policy = PolicyCfg.model_validate(_read_yaml(paths.config_dir / "policy.yaml"))
    values: dict[str, str] = {}
    if paths.secrets_file.exists():
        values.update(parse_env_file(paths.secrets_file.read_text(encoding="utf-8")))
    environ = os.environ if env is None else env
    wanted = {settings.telegram.token_env, *(p.key_env for p in llm.providers.values())}
    for key in wanted:
        if environ.get(key):
            values[key] = environ[key]
    return AppConfig(paths=paths, settings=settings, llm=llm, secrets=Secrets(values), policy=policy)
