import random

import pytest

from jobpipe.clock import parse_iso
from jobpipe.services.queue import Queue, backoff_delay


@pytest.fixture
def q(conn, clock, queue_cfg, rng):
    return Queue(conn, clock, queue_cfg, rng)


def test_backoff_bounds_and_growth():
    rng = random.Random(1)
    for attempt in range(1, 10):
        d = min(100, 10 * 2 ** (attempt - 1))
        for _ in range(50):
            v = backoff_delay(attempt, 10, 100, rng)
            assert d / 2 <= v <= d
    with pytest.raises(ValueError):
        backoff_delay(0, 10, 100, rng)


def test_enqueue_idempotent(q):
    a, created_a = q.enqueue("x", {"n": 1}, idempotency_key="k1")
    b, created_b = q.enqueue("x", {"n": 2}, idempotency_key="k1")
    assert a == b and created_a and not created_b
    assert q.get(a).payload == {"n": 1}


def test_lease_respects_due_time_and_order(q, clock):
    later, _ = q.enqueue("x", delay_s=30)
    first, _ = q.enqueue("x")
    second, _ = q.enqueue("x")
    assert q.lease("w").id == first
    assert q.lease("w").id == second
    assert q.lease("w") is None
    clock.advance(31)
    assert q.lease("w").id == later


def test_lease_filters_types(q):
    q.enqueue("a")
    b, _ = q.enqueue("b")
    assert q.lease("w", types=["b"]).id == b
    assert q.lease("w", types=["b"]) is None


def test_fail_retries_with_backoff_then_dead(q, clock):
    tid, _ = q.enqueue("x")  # max_attempts 3, base 10
    t = q.lease("w")
    out = q.fail(t.id, "boom")
    assert not out.dead and 5 <= out.retry_in_s <= 10
    task = q.get(tid)
    assert task.status == "pending" and task.attempts == 1 and task.last_error == "boom"
    assert q.lease("w") is None  # not due yet
    clock.advance(out.retry_in_s + 0.001)
    q.fail(q.lease("w").id, "boom2")
    clock.advance(100)
    out = q.fail(q.lease("w").id, "boom3")
    assert out.dead
    assert q.get(tid).status == "dead"
    assert q.counts() == {"dead": 1}
    kinds = [r[0] for r in q.conn.execute("SELECT kind FROM events WHERE entity='task' ORDER BY id")]
    assert kinds == ["created", "retry", "retry", "transition"]


def test_permanent_failure_dead_letters_at_once(q):
    tid, _ = q.enqueue("x")
    assert q.fail(q.lease("w").id, "bad input", permanent=True).dead
    assert q.get(tid).attempts == 1


def test_expired_lease_is_reclaimed_without_counting_attempt(q, clock, queue_cfg):
    tid, _ = q.enqueue("x")
    q.lease("dead-runner")
    assert q.reclaim_expired() == 0
    clock.advance(queue_cfg.lease_seconds + 1)
    assert q.reclaim_expired() == 1
    t = q.get(tid)
    assert t.status == "pending" and t.attempts == 0


def test_complete_and_release(q):
    a, _ = q.enqueue("x")
    b, _ = q.enqueue("x")
    q.complete(q.lease("w").id)
    q.release(q.lease("w").id)
    assert q.get(a).status == "done"
    assert q.get(b).status == "pending"


def test_next_run_at_is_iso_utc(q):
    tid, _ = q.enqueue("x", delay_s=60)
    assert parse_iso(q.get(tid).next_run_at).tzinfo is not None
