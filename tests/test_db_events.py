import json

import pytest

from jobpipe.adapters.storage.db import available_migrations, connect, current_version, migrate
from jobpipe.domain.errors import InvalidTransition, StaleState
from jobpipe.services.events import create_entity, transition


def test_migrate_is_idempotent(tmp_path, clock):
    c = connect(tmp_path / "x.db")
    first = migrate(c, clock)
    assert first == [name for _, name, _ in available_migrations()]
    assert migrate(c, clock) == []
    assert current_version(c) == len(available_migrations())
    assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_create_and_transition_writes_events(conn, clock):
    create_entity(conn, clock, "job", "gh:imc:1", data={"title": "C++ Engineer"})
    clock.advance(5)
    prev = transition(conn, clock, "job", "gh:imc:1", "prefiltered", actor="collect")
    assert prev == "discovered"
    row = conn.execute("SELECT state, data_json FROM jobs WHERE id = 'gh:imc:1'").fetchone()
    assert row["state"] == "prefiltered"
    assert json.loads(row["data_json"]) == {"title": "C++ Engineer"}
    ev = conn.execute("SELECT kind, from_state, to_state, actor FROM events ORDER BY id").fetchall()
    assert [tuple(e) for e in ev] == [
        ("created", None, "discovered", "system"),
        ("transition", "discovered", "prefiltered", "collect"),
    ]


def test_invalid_transition_changes_nothing(conn, clock):
    create_entity(conn, clock, "job", "j1")
    with pytest.raises(InvalidTransition):
        transition(conn, clock, "job", "j1", "shortlisted")
    assert conn.execute("SELECT state FROM jobs").fetchone()[0] == "discovered"
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1


def test_expected_state_guard(conn, clock):
    create_entity(conn, clock, "job", "j1")
    with pytest.raises(StaleState):
        transition(conn, clock, "job", "j1", "dropped", expected="prefiltered")


def test_missing_entity(conn, clock):
    with pytest.raises(KeyError):
        transition(conn, clock, "job", "nope", "dropped")


def test_application_idempotency_key_is_unique(conn, clock):
    create_entity(conn, clock, "job", "j1")
    create_entity(conn, clock, "application", "a1", columns={"job_id": "j1", "idempotency_key": "apply:imc:1"})
    with pytest.raises(Exception, match="UNIQUE"):
        create_entity(conn, clock, "application", "a2", columns={"job_id": "j1", "idempotency_key": "apply:imc:1"})
