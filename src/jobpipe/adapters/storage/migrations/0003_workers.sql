-- 0003: S4 browser workers (laptop) and their heartbeats.

CREATE TABLE workers (
  id          TEXT PRIMARY KEY,
  last_seen   TEXT NOT NULL,
  info_json   TEXT NOT NULL DEFAULT '{}'
);
