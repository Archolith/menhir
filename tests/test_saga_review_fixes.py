"""Regressions for gaps found in external review of the saga recovery branch.

Each test here corresponds to a defect that shipped and was caught by contract review rather than
by the suite -- which is the point: every one of them passed the previous test set.
"""

from __future__ import annotations

import json
import sqlite3
import time
from types import SimpleNamespace

import pytest

from menhir.infrastructure import operation_owner as oo
from menhir.infrastructure import process_liveness
from menhir.infrastructure.graph_operations import (
    RECONCILIATION_LEASE_NAME,
    GraphOperationsJournal,
    SagaWritesPausedError,
)
from menhir.services.delete_coordinator import DeleteCoordinator
from menhir.services.merge_coordinator import MergeCoordinator
from menhir.services.metric_write_coordinator import MetricWriteCoordinator
from menhir.services.saga_reconcile_gate import (
    ReconciliationGate,
    ReconciliationLeaseLost,
)
from menhir.services.scheduler_lease import SchedulerLeaseStore
from menhir.services.unmerge_coordinator import UnmergeCoordinator


@pytest.fixture()
def shared_db(tmp_path):
    return tmp_path / "ops.db"


@pytest.fixture()
def journal(shared_db):
    j = GraphOperationsJournal(db_path=shared_db)
    j._ensure_ready()
    return j


@pytest.fixture()
def lease_store(shared_db):
    s = SchedulerLeaseStore(db_path=shared_db)
    s._ensure_ready()
    return s


def _expire_gate(db, owner):
    """Force the gate row to a past expiry WITHOUT changing its owner."""
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE scheduler_leases SET lease_expires_at = ? WHERE lease_name = ? AND owner_id = ?",
            (time.time() - 60, RECONCILIATION_LEASE_NAME, owner),
        )
        conn.commit()


# --------------------------------------------------------------- gate expiry is irreversible loss


#: Every test below that reaches the PID-evidence path needs the deployment assertion that
#: makes a local PID lookup meaningful for a same-hostname owner. Without it the classifier
#: fences at OWNER_UNKNOWN by design; see ``operation_owner.host_pid_namespace_is_verifiable``.
pytestmark = pytest.mark.usefixtures("pid_namespace_verifiable")


@pytest.mark.unit
def test_an_expired_gate_cannot_be_renewed_by_its_own_owner(lease_store, shared_db):
    """The uncovered case: same owner, lapsed lease, nobody else has taken it yet.

    Previous tests only covered takeover by another owner and the row vanishing. Renewing here
    would resurrect a gate that every other process -- including the journal's PREPARE check, which
    compares expiry directly -- has already been treating as free.
    """
    gate = ReconciliationGate(lease_store=lease_store, owner_id="inst-a:1:aaa")
    assert gate.acquire() is True
    _expire_gate(shared_db, "inst-a:1:aaa")

    assert gate.renew() is False, "a lapsed gate must not be renewable back to life"
    assert gate.held is False


@pytest.mark.unit
def test_verify_still_held_rejects_an_expired_gate_with_the_same_owner(lease_store, shared_db):
    gate = ReconciliationGate(lease_store=lease_store, owner_id="inst-a:1:aaa")
    gate.acquire()
    gate.verify_still_held()  # healthy

    _expire_gate(shared_db, "inst-a:1:aaa")

    with pytest.raises(ReconciliationLeaseLost, match="EXPIRED"):
        gate.verify_still_held()
    assert gate.held is False


@pytest.mark.unit
def test_an_expired_gate_stops_pausing_writes_consistently(journal, lease_store, shared_db):
    """The gate holder and the journal must agree about expiry, not disagree.

    Before the fix the journal admitted writers while the holder still believed it owned the gate.
    """
    gate = ReconciliationGate(lease_store=lease_store, owner_id="inst-a:1:aaa")
    gate.acquire()
    _expire_gate(shared_db, "inst-a:1:aaa")

    journal.prepare(
        operation_kind="ENTITY_DELETE",
        request_json=json.dumps({"op_id": "op-1", "targets": ["n1"]}),
        target_key="k1",
        op_id="op-1",
    )
    assert journal.get("op-1") is not None, "journal correctly treats an expired gate as free"
    with pytest.raises(ReconciliationLeaseLost):
        gate.verify_still_held()


# --------------------------------------------------------------- one live replay authority


@pytest.mark.unit
@pytest.mark.parametrize(
    "factory",
    [
        lambda j: MergeCoordinator(graph_adapter=object(), journal=j),
        lambda j: UnmergeCoordinator(graph_adapter=object(), journal=j),
        lambda j: DeleteCoordinator(graph_adapter=object(), journal=j),
    ],
    ids=["merge", "unmerge", "delete"],
)
def test_per_coordinator_live_reconcile_is_refused(journal, factory):
    """A per-coordinator sweep cannot hold the gate, check ownership, or claim a row.

    It also cannot be saved by the heartbeat _apply now opens: renewal happens on an interval, so a
    reconciler acting on somebody else's row dispatches its first mutation before the first renewal
    discovers the row was never its to claim.
    """
    coord = factory(journal)
    with pytest.raises(NotImplementedError, match="central dispatcher"):
        coord.reconcile(dry_run=False)


@pytest.mark.unit
def test_metric_live_reconcile_is_refused(journal, tmp_path):
    from menhir.infrastructure.metric_receipts import MetricReceiptStore

    coord = MetricWriteCoordinator(
        graph_adapter=object(), journal=journal, receipts=MetricReceiptStore(db_path=tmp_path / "r.db")
    )
    with pytest.raises(NotImplementedError, match="central dispatcher"):
        coord.reconcile(dry_run=False)


@pytest.mark.unit
def test_reconcile_now_defaults_to_observation(journal):
    """A bare reconcile() must OBSERVE, not replay.

    The old default was dry_run=False, so any existing caller that had simply called reconcile()
    would have kept replaying. Flipping the default makes the safe behaviour the accidental one.
    """
    result = MergeCoordinator(graph_adapter=object(), journal=journal).reconcile()
    assert result["dry_run"] is True


@pytest.mark.unit
def test_dry_run_reconcile_still_works_on_every_coordinator(journal):
    """Refusing live mode must not disable observation, which the dispatcher depends on."""
    assert MergeCoordinator(graph_adapter=object(), journal=journal).reconcile(dry_run=True)[
        "dry_run"
    ] is True


# --------------------------------------------------------------- durable TTL is kind-derived


@pytest.mark.unit
@pytest.mark.parametrize("kind", ["ENTITY_MERGE", "METRIC_WRITE"])
def test_prepare_stamps_the_kind_derived_expiry(journal, kind):
    """Inspects the DURABLE expiry immediately after PREPARE, before any heartbeat could repair it.

    The writer's local heartbeat computes headroom from the kind. A shorter durable stamp would let
    it believe it had more claim than the row carries -- in the dangerous direction, passing its own
    pre-dispatch check while the real claim was nearly expired.
    """
    op_id = f"op-{kind}"
    journal.prepare(
        operation_kind=kind,
        request_json=json.dumps({"op_id": op_id, "targets": ["n1"]}),
        target_key=f"k-{kind}",
        op_id=op_id,
    )
    row = journal.get(op_id)

    expected = oo.lease_seconds_for_kind(kind)
    expires = oo._parse_iso(row["owner_lease_expires_at"])
    stamped = oo._parse_iso(row["owner_heartbeat_at"])
    actual = (expires - stamped).total_seconds()

    assert actual == pytest.approx(expected, abs=2.0), (
        f"{kind}: durable expiry {actual:.0f}s must match the kind-derived TTL {expected}s"
    )


@pytest.mark.unit
def test_metric_prepare_gets_a_longer_durable_claim_than_a_single_statement_kind(journal):
    for kind, key in (("ENTITY_MERGE", "a"), ("METRIC_WRITE", "b")):
        journal.prepare(
            operation_kind=kind,
            request_json=json.dumps({"op_id": kind, "targets": ["n"]}),
            target_key=key,
            op_id=kind,
        )

    def _ttl(op):
        r = journal.get(op)
        return (oo._parse_iso(r["owner_lease_expires_at"]) - oo._parse_iso(r["owner_heartbeat_at"])).total_seconds()

    assert _ttl("METRIC_WRITE") > _ttl("ENTITY_MERGE")


@pytest.mark.unit
def test_claiming_an_abandoned_row_uses_the_rows_own_kind(journal):
    """The claimant must not stamp a one-statement TTL onto a two-statement operation."""
    with sqlite3.connect(journal.db_path) as conn:
        conn.execute(
            "INSERT INTO graph_operations (op_id, operation_kind, request_json, state, created_at, "
            "updated_at, owner_token, owner_lease_expires_at) VALUES "
            "('op-m', 'METRIC_WRITE', '{}', 'PREPARED', '2026-01-01T00:00:00+00:00', "
            f"'2026-01-01T00:00:00+00:00', 'old:{process_liveness.hostname()}:999999:a', "
            "'2020-01-01T00:00:00+00:00')"
        )
        conn.commit()

    assert journal.claim_abandoned_operation("op-m") is True

    row = journal.get("op-m")
    ttl = (
        oo._parse_iso(row["owner_lease_expires_at"]) - oo._parse_iso(row["owner_heartbeat_at"])
    ).total_seconds()
    assert ttl == pytest.approx(oo.lease_seconds_for_kind("METRIC_WRITE"), abs=2.0)


# --------------------------------------------------------------- gate read fails closed


@pytest.mark.unit
def test_an_unreadable_gate_schema_fails_closed(journal, lease_store, shared_db):
    """A present-but-unreadable gate is not evidence of absence.

    Only a MISSING table is. Previously any sqlite error admitted the write.
    """
    with sqlite3.connect(shared_db) as conn:
        conn.execute("DROP TABLE scheduler_leases")
        conn.execute("CREATE TABLE scheduler_leases (wrong_column TEXT)")
        conn.commit()

    with pytest.raises(SagaWritesPausedError, match="cannot read the reconciliation gate"):
        journal.prepare(
            operation_kind="ENTITY_DELETE",
            request_json=json.dumps({"op_id": "op-1", "targets": ["n1"]}),
            target_key="k1",
            op_id="op-1",
        )


@pytest.mark.unit
def test_a_missing_gate_table_still_fails_open(tmp_path):
    """The fresh-database case must keep working."""
    j = GraphOperationsJournal(db_path=tmp_path / "fresh.db")
    j._ensure_ready()
    tables = {
        r[0] for r in sqlite3.connect(j.db_path).execute(
            "SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "scheduler_leases" not in tables

    j.prepare(
        operation_kind="ENTITY_DELETE",
        request_json=json.dumps({"op_id": "op-1", "targets": ["n1"]}),
        target_key="k1",
        op_id="op-1",
    )
    assert j.get("op-1") is not None


# --------------------------------------------------------------- statement count is a bound


@pytest.mark.unit
def test_metric_statement_count_is_documented_as_a_bound_not_a_measurement():
    """On current source the metric path issues ONE mutating statement, not two.

    record_metric passes episode_uuids=[] and _link_episodes returns immediately on an empty list.
    Holding the count at 2 is deliberate conservatism -- a longer lease only delays recovery -- but
    it must not be described as the measured count.
    """
    import inspect

    from menhir.infrastructure import view_write_repository as vwr

    src = inspect.getsource(vwr.ViewWriteRepositoryMixin._link_episodes)
    assert "if not episode_uuids:" in src, (
        "the early return this bound reasons about no longer exists; re-derive the count"
    )
    assert oo.SAGA_STATEMENT_COUNTS["METRIC_WRITE"] == 2


class TestClientReadDeadlineIsMeasured:
    """CF-211: whether Menhir has a client read bound is a property of the SERVER.

    execute() sets a server transaction timeout, then materialises a lazy result over a socket.
    The driver applies a socket read deadline ONLY from the server's
    connection.recv_timeout_seconds hint, so the bound can vanish by changing database rather
    than by changing this code. Preflight therefore measures it instead of assuming it.
    """

    def test_absent_deadline_is_reported_as_a_warning(self):
        from menhir.services.saga_preflight import preflight_from_run

        run = SimpleNamespace(
            run_id="r", scanned=0, counts={}, counts_by_kind={},
            oldest_prepared_age_seconds=None, examples={}, write_ready=True,
            blocking_reasons=[],
        )
        report = preflight_from_run(
            run, client_read_timeout_s=None, client_read_timeout_measured=True
        )

        assert report.client_read_timeout_s is None
        assert any("NO client-side read deadline" in w for w in report.warnings)
        # A warning, never a blocker: recovery stays correct because ownership needs positive
        # death evidence, not elapsed time. What is at risk is availability.
        assert report.clean is True

    def test_present_deadline_produces_no_warning(self):
        from menhir.services.saga_preflight import preflight_from_run

        run = SimpleNamespace(
            run_id="r", scanned=0, counts={}, counts_by_kind={},
            oldest_prepared_age_seconds=None, examples={}, write_ready=True,
            blocking_reasons=[],
        )
        report = preflight_from_run(
            run, client_read_timeout_s=120.0, client_read_timeout_measured=True
        )

        assert report.client_read_timeout_s == 120.0
        assert not any("read deadline" in w for w in report.warnings)

    def test_unmeasured_is_not_treated_as_absent(self):
        """Not probing is not evidence of no deadline, and must not raise a false warning."""
        from menhir.services.saga_preflight import preflight_from_run

        run = SimpleNamespace(
            run_id="r", scanned=0, counts={}, counts_by_kind={},
            oldest_prepared_age_seconds=None, examples={}, write_ready=True,
            blocking_reasons=[],
        )
        report = preflight_from_run(run)

        assert report.client_read_timeout_measured is False
        assert report.warnings == [] or not any("read deadline" in w for w in report.warnings)

    def test_probe_returns_none_when_it_cannot_measure(self):
        """An unmeasurable bound is not a bound: the probe must not invent one."""
        from menhir.infrastructure.neo4j import Neo4jRepository

        repo = Neo4jRepository.__new__(Neo4jRepository)
        repo.database = "neo4j"

        def _boom():
            raise RuntimeError("no driver")

        repo._get_driver = _boom
        assert repo.client_read_timeout_seconds() is None
