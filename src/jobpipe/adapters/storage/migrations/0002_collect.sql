-- 0002: S1 collection. Searchable job columns, per-run source stats, per-source cursors.

ALTER TABLE jobs ADD COLUMN source TEXT;
ALTER TABLE jobs ADD COLUMN kind TEXT;                 -- ats | aggregator | community
ALTER TABLE jobs ADD COLUMN company TEXT;
ALTER TABLE jobs ADD COLUMN title TEXT;
ALTER TABLE jobs ADD COLUMN url TEXT;
ALTER TABLE jobs ADD COLUMN location TEXT;
ALTER TABLE jobs ADD COLUMN country TEXT;
ALTER TABLE jobs ADD COLUMN posted_at TEXT;
ALTER TABLE jobs ADD COLUMN fingerprint TEXT;
ALTER TABLE jobs ADD COLUMN drop_rule TEXT;            -- why the prefilter or dedupe dropped it
ALTER TABLE jobs ADD COLUMN duplicate_of TEXT;         -- jobs.id of the canonical copy
ALTER TABLE jobs ADD COLUMN last_seen TEXT;
ALTER TABLE jobs ADD COLUMN digested_at TEXT;          -- sent in a daily digest (never twice)

CREATE INDEX jobs_fingerprint ON jobs(fingerprint);
CREATE INDEX jobs_source ON jobs(source);
CREATE INDEX jobs_digest ON jobs(state, digested_at);

CREATE TABLE source_runs (
  id          INTEGER PRIMARY KEY,
  source      TEXT NOT NULL,
  started_at  TEXT NOT NULL,
  finished_at TEXT,
  status      TEXT NOT NULL,     -- ok | partial | blocked | error | skipped
  fetched     INTEGER NOT NULL DEFAULT 0,
  new         INTEGER NOT NULL DEFAULT 0,
  kept        INTEGER NOT NULL DEFAULT 0,
  dropped     INTEGER NOT NULL DEFAULT 0,
  closed      INTEGER NOT NULL DEFAULT 0,
  error       TEXT
);
CREATE INDEX source_runs_source ON source_runs(source, started_at);

CREATE TABLE source_cursors (
  source  TEXT NOT NULL,
  key     TEXT NOT NULL,
  value   TEXT NOT NULL,
  PRIMARY KEY (source, key)
);
