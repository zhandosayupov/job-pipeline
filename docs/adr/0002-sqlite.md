# 0002. SQLite in WAL mode, numbered SQL migrations, repositories

Date: 2026-11-26 · Status: accepted

## Context
No room for Postgres or Redis on 1 GB. Writers are few (the bot and one or two oneshot runs).

## Decision
* One SQLite file, `journal_mode=WAL`, `busy_timeout=5000`, `foreign_keys=ON`, `synchronous=NORMAL`.
* Migrations are `NNNN_name.sql` files in the package, applied in order inside a transaction,
  tracked in `schema_migrations`. Every process runs `migrate()` on start; it is a no-op when current.
* Services talk to repositories, not to SQL, so a Postgres adapter can replace SQLite later.
* Timestamps are ISO-8601 UTC strings with a fixed format (`2026-11-26T10:00:00.000000Z`) so they
  compare correctly as text.

## Consequences
* Backups use the SQLite online backup API, never a file copy of a live database.
* `UPDATE ... RETURNING` needs SQLite 3.35 or newer; Python 3.12 builds ship newer versions.
