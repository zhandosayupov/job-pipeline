-- 0001: core tables for S0. Later sprints add columns and tables in new migrations.

CREATE TABLE events (
  id          INTEGER PRIMARY KEY,
  ts          TEXT NOT NULL,
  entity      TEXT NOT NULL,          -- job | application | conversation | task | control | ...
  entity_id   TEXT NOT NULL,
  kind        TEXT NOT NULL,          -- transition | created | note | ...
  from_state  TEXT,
  to_state    TEXT,
  actor       TEXT NOT NULL,          -- system | owner | worker | <command>
  data_json   TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX events_entity ON events(entity, entity_id);
CREATE INDEX events_ts ON events(ts);

CREATE TABLE jobs (
  id          TEXT PRIMARY KEY,
  state       TEXT NOT NULL,
  protected   INTEGER NOT NULL DEFAULT 0,
  data_json   TEXT NOT NULL DEFAULT '{}',
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);
CREATE INDEX jobs_state ON jobs(state);

CREATE TABLE applications (
  id               TEXT PRIMARY KEY,
  job_id           TEXT NOT NULL REFERENCES jobs(id),
  state            TEXT NOT NULL,
  idempotency_key  TEXT NOT NULL UNIQUE,
  data_json        TEXT NOT NULL DEFAULT '{}',
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL
);
CREATE INDEX applications_state ON applications(state);

CREATE TABLE conversations (
  id              TEXT PRIMARY KEY,
  application_id  TEXT REFERENCES applications(id),
  channel         TEXT NOT NULL,      -- gmail | hh | telegram
  state           TEXT NOT NULL,
  data_json       TEXT NOT NULL DEFAULT '{}',
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL
);

CREATE TABLE tasks (
  id               INTEGER PRIMARY KEY,
  type             TEXT NOT NULL,
  payload_json     TEXT NOT NULL DEFAULT '{}',
  status           TEXT NOT NULL CHECK (status IN ('pending','running','done','dead')),
  attempts         INTEGER NOT NULL DEFAULT 0,
  max_attempts     INTEGER NOT NULL,
  next_run_at      TEXT NOT NULL,
  idempotency_key  TEXT UNIQUE,
  last_error       TEXT,
  lease_owner      TEXT,
  lease_until      TEXT,
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL
);
CREATE INDEX tasks_due ON tasks(status, next_run_at);

CREATE TABLE side_effects (
  key         TEXT PRIMARY KEY,
  kind        TEXT NOT NULL,
  status      TEXT NOT NULL CHECK (status IN ('planned','done','unknown','failed')),
  run_id      INTEGER,                -- runs.id of the process that planned it
  data_json   TEXT NOT NULL DEFAULT '{}',
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);

CREATE TABLE control (
  key         TEXT PRIMARY KEY,
  value       TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);

CREATE TABLE runs (
  id           INTEGER PRIMARY KEY,
  command      TEXT NOT NULL,
  pid          INTEGER NOT NULL,
  host         TEXT NOT NULL,
  status       TEXT NOT NULL CHECK (status IN ('running','ok','failed','killed','stopped')),
  started_at   TEXT NOT NULL,
  finished_at  TEXT,
  info_json    TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX runs_status ON runs(status);

CREATE TABLE llm_calls (
  id              INTEGER PRIMARY KEY,
  ts              TEXT NOT NULL,
  task            TEXT NOT NULL,
  provider        TEXT NOT NULL,
  model           TEXT NOT NULL,
  prompt_version  TEXT NOT NULL,
  status          TEXT NOT NULL,      -- ok | rate_limited | error | invalid_output | timeout
  tokens_in       INTEGER NOT NULL DEFAULT 0,
  tokens_out      INTEGER NOT NULL DEFAULT 0,
  latency_ms      INTEGER NOT NULL DEFAULT 0,
  cost_usd        REAL NOT NULL DEFAULT 0,
  error           TEXT
);
CREATE INDEX llm_calls_provider_ts ON llm_calls(provider, ts);

CREATE TABLE provider_state (
  provider              TEXT PRIMARY KEY,
  cooldown_until        TEXT,
  consecutive_failures  INTEGER NOT NULL DEFAULT 0,
  breaker_until         TEXT
);
