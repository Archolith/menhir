"""CF-211 part 2: the writer-side ownership heartbeat.

The renewing half is bookkeeping. The half that protects the graph is the negative one: a writer
that has LOST its claim must start nothing new, because the row may already belong to a reconciler
that has begun replaying it. So most of these tests are about what stops, not what continues.

Timing is kept out of the assertions wherever possible -- the loop is driven by a stubbed journal
and a short lease rather than by sleeping through a real interval, so nothing here depends on the
machine being fast.
"""

from __future__ import annotations

import threading
import time

import pytest

from menhir.infrastructure import neo4j as n4
from menhir.services.saga_writer_heartbeat import (
    SagaOwnershipLost,
    WriterHeartbeat,
    writer_heartbeat,
)


class _Journal:
    """Stub journal whose renewal result the test controls."""

    def __init__(self, *, result=True, raises=None):
        self.result = result
        self.raises = raises
        self.calls: list[tuple[str, int, str]] = []
        self.renewed = threading.Event()

    def renew_owner_heartbeat(self, op_id, *, seconds, owner_token):
        self.calls.append((op_id, seconds, owner_token))
        self.renewed.set()
        if self.raises is not None:
            raise self.raises
        return self.result


def _wait(event: threading.Event, timeout: float = 5.0) -> bool:
    return event.wait(timeout=timeout)


# --------------------------------------------------------------------------- renewing


@pytest.mark.unit
def test_it_renews_with_its_own_token_and_lease(monkeypatch):
    journal = _Journal(result=True)
    beat = WriterHeartbeat(journal, "op-1", lease_seconds=3, owner_token="inst:1:aaa")
    beat.start()
    try:
        assert _wait(journal.renewed), "the heartbeat must renew without being prompted"
    finally:
        beat.stop()

    op_id, seconds, token = journal.calls[0]
    assert (op_id, seconds, token) == ("op-1", 3, "inst:1:aaa")
    assert beat.lost is False


@pytest.mark.unit
def test_the_renewal_interval_leaves_room_for_a_missed_tick():
    """Renewing at half-life leaves no margin -- one missed tick and the row is already claimable."""
    beat = WriterHeartbeat(_Journal(), "op-1", lease_seconds=120)

    assert beat._interval < 120 / 2, "must renew before half-life"
    assert beat._interval == pytest.approx(40.0)


# --------------------------------------------------------------------------- losing the claim


@pytest.mark.unit
def test_a_returned_false_marks_the_claim_lost_immediately():
    """False is PROOF another owner holds the row: no retry budget, no ambiguity."""
    journal = _Journal(result=False)
    beat = WriterHeartbeat(journal, "op-1", lease_seconds=3)
    beat.start()
    try:
        deadline = time.monotonic() + 5.0
        while not beat.lost and time.monotonic() < deadline:
            time.sleep(0.05)
    finally:
        beat.stop()

    assert beat.lost is True
    assert beat.should_continue() is False
    assert len(journal.calls) == 1, "it must stop renewing once the claim is proven gone"


@pytest.mark.unit
def test_a_raising_renewal_is_tolerated_briefly_then_fails_closed():
    """A raised renewal is ambiguous (the sidecar may be briefly locked), unlike a returned False.

    Ambiguity gets a small budget; it must NOT get an unlimited one, because continuing to mutate
    on an unverifiable claim is the outcome with the worse failure mode.
    """
    journal = _Journal(raises=RuntimeError("db locked"))
    beat = WriterHeartbeat(journal, "op-1", lease_seconds=3)
    beat.start()
    try:
        deadline = time.monotonic() + 10.0
        while not beat.lost and time.monotonic() < deadline:
            time.sleep(0.05)
    finally:
        beat.stop()

    assert beat.lost is True, "repeated unverifiable renewals must fail closed"
    assert len(journal.calls) >= 3, "it must not give up on the first ambiguous failure"


@pytest.mark.unit
def test_lost_latches_and_is_never_silently_reacquired():
    """A reconciler may already be replaying the row; taking the claim back would hide that."""
    journal = _Journal(result=False)
    beat = WriterHeartbeat(journal, "op-1", lease_seconds=3)
    beat.start()
    try:
        deadline = time.monotonic() + 5.0
        while not beat.lost and time.monotonic() < deadline:
            time.sleep(0.05)
    finally:
        beat.stop()

    assert beat.lost is True
    journal.result = True  # the row becomes ours again as far as the journal is concerned
    assert beat.lost is True, "loss must latch"
    assert beat.should_continue() is False


@pytest.mark.unit
def test_raise_if_lost_aborts_the_writer_path():
    beat = WriterHeartbeat(_Journal(), "op-1", lease_seconds=60)
    beat.raise_if_lost()  # not lost: no-op

    beat._lost.set()
    with pytest.raises(SagaOwnershipLost, match="op-1"):
        beat.raise_if_lost()


# --------------------------------------------------------------------------- lifecycle


@pytest.mark.unit
def test_the_context_manager_stops_the_thread_even_when_the_body_raises():
    journal = _Journal()
    captured: list[WriterHeartbeat] = []

    with pytest.raises(RuntimeError, match="writer blew up"):
        with writer_heartbeat(journal, "op-1", lease_seconds=3) as beat:
            captured.append(beat)
            raise RuntimeError("writer blew up")

    assert captured[0]._thread is not None
    assert not captured[0]._thread.is_alive(), "the heartbeat thread must not outlive the block"


@pytest.mark.unit
def test_disabled_yields_none_so_not_heartbeating_is_visible():
    with writer_heartbeat(_Journal(), "op-1", enabled=False) as beat:
        assert beat is None, (
            "a no-op object would look like a heartbeat that never fails; None keeps the absence "
            "visible at the call site"
        )


@pytest.mark.unit
def test_stop_is_safe_before_start_and_twice():
    beat = WriterHeartbeat(_Journal(), "op-1", lease_seconds=3)
    beat.stop()
    beat.start()
    beat.stop()
    beat.stop()


@pytest.mark.unit
def test_starting_twice_is_refused():
    beat = WriterHeartbeat(_Journal(), "op-1", lease_seconds=60)
    beat.start()
    try:
        with pytest.raises(RuntimeError, match="already started"):
            beat.start()
    finally:
        beat.stop()


@pytest.mark.unit
def test_the_thread_is_a_daemon_so_it_cannot_wedge_shutdown():
    beat = WriterHeartbeat(_Journal(), "op-1", lease_seconds=60)
    beat.start()
    try:
        assert beat._thread.daemon is True
    finally:
        beat.stop()


# --------------------------------------------------------------------------- the driver seam


class _FakeSession:
    def __init__(self, recorder):
        self._recorder = recorder

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def run(self, query, **kwargs):
        self._recorder.append(query)
        return []


class _FakeDriver:
    def __init__(self, recorder):
        self._recorder = recorder

    def session(self, **_kw):
        return _FakeSession(self._recorder)


@pytest.fixture()
def repo_and_calls(monkeypatch):
    calls: list = []
    repo = n4.Neo4jRepository(uri="bolt://x", database="neo4j", user="u", password="p")
    monkeypatch.setattr(repo, "_get_driver", lambda: _FakeDriver(calls))
    return repo, calls


@pytest.mark.unit
def test_a_live_claim_lets_the_statement_dispatch(repo_and_calls):
    repo, calls = repo_and_calls
    repo.execute("MATCH (n) RETURN n", should_continue=lambda: True)

    assert len(calls) == 1


@pytest.mark.unit
def test_a_lost_claim_refuses_to_dispatch_at_all(repo_and_calls):
    """Checked before the FIRST attempt too: the first statement can double-apply as easily."""
    repo, calls = repo_and_calls

    with pytest.raises(n4.SagaOwnershipRevoked):
        repo.execute("MATCH (n) RETURN n", should_continue=lambda: False)

    assert calls == [], "nothing may be sent to the driver once ownership is gone"


@pytest.mark.unit
def test_a_claim_lost_mid_retry_stops_further_attempts(repo_and_calls, monkeypatch):
    """The case the seam exists for: the first attempt fails transiently, then the claim goes."""
    repo, calls = repo_and_calls
    alive = {"ok": True}

    class _FlakyDriver(_FakeDriver):
        def session(self, **_kw):
            alive["ok"] = False  # the claim is lost while attempt 1 is in flight
            raise n4.ServiceUnavailable("transient")

    monkeypatch.setattr(repo, "_get_driver", lambda: _FlakyDriver(calls))
    monkeypatch.setattr(n4.time, "sleep", lambda _s: None)

    with pytest.raises(n4.SagaOwnershipRevoked):
        repo.execute("MATCH (n) RETURN n", should_continue=lambda: alive["ok"])


@pytest.mark.unit
def test_without_a_predicate_behaviour_is_unchanged(repo_and_calls):
    """Every non-saga caller must be untouched by this seam."""
    repo, calls = repo_and_calls
    repo.execute("MATCH (n) RETURN n")

    assert len(calls) == 1


@pytest.mark.unit
def test_a_heartbeat_can_be_handed_straight_to_execute(repo_and_calls):
    """The intended wiring: the writer passes its own heartbeat as the predicate."""
    repo, calls = repo_and_calls
    beat = WriterHeartbeat(_Journal(), "op-1", lease_seconds=60)

    repo.execute("MATCH (n) RETURN n", should_continue=beat.should_continue)
    assert len(calls) == 1

    beat._lost.set()
    with pytest.raises(n4.SagaOwnershipRevoked):
        repo.execute("MATCH (n) RETURN n", should_continue=beat.should_continue)
    assert len(calls) == 1, "no further statement may be dispatched"
