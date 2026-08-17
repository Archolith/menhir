"""CF-20c: the reconciliation gate and the global PREPARE pause.

The gate has to do two separable things, and both are tested here:

* be exclusive between reconcilers, so two instances restarting together cannot both replay a row;
* pause new saga PREPARE across the deployment, so a writer cannot add a PREPARED row after
  recovery has already decided what the backlog contains.

The second is the one with teeth, because it changes the behaviour of every saga writer. It works
only because the lease row and the journal live in the same SQLite database and both sides take the
same write lock -- so these tests put them on one file, as production does.

Nothing in CF-20c-1 replays. Holding the gate is what makes replay safe to attempt later.
"""

from __future__ import annotations

import json
import sqlite3
import time

import pytest

from menhir.infrastructure.graph_operations import (
    RECONCILIATION_LEASE_NAME,
    GraphOperationError,
    GraphOperationsJournal,
    SagaWritesPausedError,
)
from menhir.services.saga_reconcile_gate import (
    ReconciliationGate,
    ReconciliationLeaseLost,
    reconciliation_gate,
)
from menhir.services.scheduler_lease import SchedulerLeaseStore


@pytest.fixture()
def shared_db(tmp_path):
    """One database for both the journal and the lease, as in production."""
    return tmp_path / "ops.db"


@pytest.fixture()
def journal(shared_db):
    j = GraphOperationsJournal(db_path=shared_db)
    j._ensure_ready()
    return j


@pytest.fixture()
def lease_store(shared_db):
    store = SchedulerLeaseStore(db_path=shared_db)
    store._ensure_ready()
    return store


def _prepare(journal, op_id="op-1", **kw):
    return journal.prepare(
        operation_kind="ENTITY_DELETE",
        request_json=json.dumps({"op_id": op_id, "targets": [f"n-{op_id}"]}),
        target_key=f"key-{op_id}",
        op_id=op_id,
        **kw,
    )


def _write_lease(db, *, owner="other:1:abc", expires_at):
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO scheduler_leases (lease_name, owner_id, owner_pid, hostname, "
            "started_at, heartbeat_at, lease_expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (RECONCILIATION_LEASE_NAME, owner, 4242, "host",
             "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00", expires_at),
        )
        conn.commit()


# --------------------------------------------------------------------------- the PREPARE pause


@pytest.mark.unit
def test_a_held_gate_pauses_new_saga_prepare(journal, lease_store, shared_db):
    _write_lease(shared_db, expires_at=time.time() + 300)

    with pytest.raises(SagaWritesPausedError) as excinfo:
        _prepare(journal)

    assert "recovery" in str(excinfo.value).lower()
    assert journal.get("op-1") is None, "no journal row may be created while writes are paused"


@pytest.mark.unit
def test_prepare_is_admitted_again_once_the_gate_is_released(journal, lease_store, shared_db):
    gate = ReconciliationGate(lease_store=lease_store)
    assert gate.acquire() is True
    with pytest.raises(SagaWritesPausedError):
        _prepare(journal, op_id="blocked")

    gate.release()
    _prepare(journal, op_id="allowed")

    assert journal.get("allowed") is not None
    assert journal.get("blocked") is None


@pytest.mark.unit
def test_an_expired_gate_does_not_pause_writes(journal, lease_store, shared_db):
    """A hard-killed reconciler must stop pausing writes on its own, without operator action."""
    _write_lease(shared_db, expires_at=time.time() - 300)

    _prepare(journal)

    assert journal.get("op-1") is not None


@pytest.mark.unit
def test_a_caller_supplied_connection_is_gated_too(journal, lease_store, shared_db):
    """The metric saga hands in its own connection; it must not be a way around the pause."""
    _write_lease(shared_db, expires_at=time.time() + 300)

    with sqlite3.connect(shared_db) as conn:
        with pytest.raises(SagaWritesPausedError):
            _prepare(journal, op_id="metric-ish", conn=conn)


@pytest.mark.unit
def test_saga_writes_paused_is_a_graph_operation_error(journal, lease_store, shared_db):
    """Existing callers catch GraphOperationError; the new error must not slip past them."""
    _write_lease(shared_db, expires_at=time.time() + 300)

    with pytest.raises(GraphOperationError):
        _prepare(journal)


# --------------------------------------------------------------------------- fail-open vs closed


@pytest.mark.unit
def test_a_missing_lease_table_does_not_pause_writes(tmp_path):
    """The normal state of a fresh database. A gate that has never existed cannot be held."""
    db = tmp_path / "fresh.db"
    journal = GraphOperationsJournal(db_path=db)
    journal._ensure_ready()

    tables = {
        r[0] for r in sqlite3.connect(db).execute(
            "SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "scheduler_leases" not in tables, "precondition: the journal must not create that table"

    _prepare(journal)
    assert journal.get("op-1") is not None


@pytest.mark.unit
def test_a_lease_row_with_an_unreadable_expiry_fails_closed(journal, lease_store, shared_db):
    """A PRESENT row is positive evidence recovery is running; it cannot be proven expired.

    Contrast with the missing table, which is proof no gate was ever created. The asymmetry is
    deliberate: absence of the mechanism is safe, an unreadable instance of it is not.
    """
    _write_lease(shared_db, expires_at="not-a-number")

    with pytest.raises(SagaWritesPausedError, match="unreadable expiry"):
        _prepare(journal)


# --------------------------------------------------------------------------- exclusivity


@pytest.mark.unit
def test_a_second_reconciler_cannot_take_a_held_gate(lease_store):
    first = ReconciliationGate(lease_store=lease_store, owner_id="inst-a:1:aaa")
    second = ReconciliationGate(lease_store=lease_store, owner_id="inst-b:2:bbb")

    assert first.acquire() is True
    assert second.acquire() is False, "two reconcilers must never both own the backlog"
    assert second.held is False


@pytest.mark.unit
def test_losing_the_gate_is_reported_rather_than_silently_retaken(lease_store, shared_db):
    """renew must not re-acquire: the new owner may already be replaying the same rows."""
    gate = ReconciliationGate(lease_store=lease_store, owner_id="inst-a:1:aaa")
    assert gate.acquire() is True

    _write_lease(shared_db, owner="inst-b:2:bbb", expires_at=time.time() + 300)

    assert gate.renew() is False
    assert gate.held is False


@pytest.mark.unit
def test_verify_still_held_raises_when_ownership_moved(lease_store, shared_db):
    gate = ReconciliationGate(lease_store=lease_store, owner_id="inst-a:1:aaa")
    gate.acquire()
    gate.verify_still_held()  # still ours

    _write_lease(shared_db, owner="inst-b:2:bbb", expires_at=time.time() + 300)

    with pytest.raises(ReconciliationLeaseLost):
        gate.verify_still_held()


@pytest.mark.unit
def test_verify_still_held_raises_when_the_gate_vanished(lease_store):
    gate = ReconciliationGate(lease_store=lease_store, owner_id="inst-a:1:aaa")
    gate.acquire()
    gate.release()

    with pytest.raises(ReconciliationLeaseLost):
        gate.verify_still_held()


# --------------------------------------------------------------------------- the context manager


@pytest.mark.unit
def test_the_context_manager_releases_even_when_the_body_raises(journal, lease_store):
    """A leaked gate pauses every saga writer until TTL. A crashed pass must not become an outage."""
    with pytest.raises(RuntimeError, match="recovery blew up"):
        with reconciliation_gate(lease_store=lease_store) as gate:
            assert gate is not None
            raise RuntimeError("recovery blew up")

    _prepare(journal)
    assert journal.get("op-1") is not None, "writes must be admitted after a failed pass"


@pytest.mark.unit
def test_the_context_manager_yields_none_when_the_gate_is_taken(lease_store, shared_db):
    _write_lease(shared_db, owner="inst-b:2:bbb", expires_at=time.time() + 300)

    with reconciliation_gate(lease_store=lease_store) as gate:
        assert gate is None, "a caller must be able to skip recovery without catching anything"


@pytest.mark.unit
def test_the_context_manager_pauses_writes_only_inside_the_block(journal, lease_store):
    with reconciliation_gate(lease_store=lease_store) as gate:
        assert gate is not None
        with pytest.raises(SagaWritesPausedError):
            _prepare(journal, op_id="inside")

    _prepare(journal, op_id="outside")
    assert journal.get("outside") is not None
    assert journal.get("inside") is None
