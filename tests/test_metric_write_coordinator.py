"""MetricWriteCoordinator saga tests (Plan 2 step 3).

Covers the PREPARED -> MUTATE -> VERIFY -> COMMITTED sequence and, critically, the crash-recovery
cases the step-1 review flagged as deferred to this step: a crash after PREPARED but before the
graph write, and a crash after the graph write but before COMMITTED. Reconciliation must converge
in both without double-effect, and drift must land in NEEDS_REVIEW without mutating.

Offline: a fake graph adapter stands in for Neo4j; SQLite is a real temp DB.
"""

from __future__ import annotations

import sqlite3

import pytest

from menhir.infrastructure.graph_operations import GraphOperationsJournal
from menhir.infrastructure.metric_receipts import MetricReceiptStore
from menhir.services.metric_write_coordinator import (
    MetricDrift,
    MetricWriteCoordinator,
    chain_digest,
    state_fingerprint,
)


class FakeGraph:
    """In-memory stand-in for the :Metric graph. Mirrors record_metric's idempotent semantics."""

    def __init__(self) -> None:
        self.metrics: dict[str, dict] = {}  # view_key -> {uuid, value, receipt_op_id}
        self.write_calls = 0
        self.fail_next = False

    @staticmethod
    def _key(namespace, subject, counter):
        return f"{(namespace or '').strip()}::{subject.strip().lower()}::{counter.strip().lower()}"

    def record_metric(self, *, subject, counter, value, receipt_op_id, namespace=None,
                      valid_at=None, source="instrumentation", node_uuid=None, **_):
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("neo4j unavailable")
        self.write_calls += 1
        key = self._key(namespace, subject, counter)
        existing = self.metrics.get(key)
        # Mirror ViewRepository exactly:
        #  - unchanged value -> NO new version; refresh in place, but DO repoint the receipt id
        #    (plan A4). Getting this wrong in the real repo caused a false-drift lockout, and a
        #    fake that silently did the right thing is what hid it -- keep them in lockstep.
        #  - changed value   -> new version node, honouring the caller's frozen node_uuid
        #    (the anti-fork contract).
        if existing and float(existing["value"]) == float(value):
            uuid = existing["uuid"]
        else:
            uuid = node_uuid or f"m-{len(self.metrics) + 1}"
        self.metrics[key] = {"uuid": uuid, "value": float(value), "receipt_op_id": receipt_op_id}
        return {"uuid": uuid, "view_key": key, "created": existing is None}

    def fetch_metric(self, *, subject, counter, namespace=None):
        got = self.metrics.get(self._key(namespace, subject, counter))
        return dict(got) if got else None

    def fetch_metric_state(self, *, view_key):
        """Mirror ViewRepository.fetch_metric_state: the full protected state, incl. labels/type."""
        got = self.metrics.get(view_key)
        if not got:
            return None
        return {
            "uuid": got["uuid"],
            "value": float(got["value"]),
            "type": got.get("type", "METRIC"),
            "labels": got.get("labels", ["Metric"]),
            "view_current": True,
            "receipt_op_id": got.get("receipt_op_id"),
        }


@pytest.fixture
def coord(tmp_path):
    db = tmp_path / "saga.db"
    graph = FakeGraph()
    c = MetricWriteCoordinator(
        graph_adapter=graph,
        journal=GraphOperationsJournal(db_path=db),
        receipts=MetricReceiptStore(db_path=db),
        telemetry_db_path=db,
    )
    c.journal._ensure_ready()
    c.receipts._ensure_ready()
    return c


# ------------------------------------------------------------------ happy path


@pytest.mark.unit
def test_run_tally_commits_with_receipt(coord):
    res = coord.record_run_tally(subject="perception", counter="abstained", value=3.0, namespace="ns")
    op = res["op_id"]

    assert coord.journal.get(op)["state"] == "COMMITTED"
    receipt = coord.receipts.get(op)
    assert receipt["receipt_kind"] == "RUN_TALLY"
    assert receipt["aggregate_value"] == 3.0
    assert receipt["source_table"] is None  # a run tally claims no fold lineage
    assert coord.graph_adapter.metrics["ns::perception::abstained"]["value"] == 3.0


@pytest.mark.unit
def test_telemetry_fold_accumulates_across_receipts(coord):
    """Chained accumulator: the second fold's aggregate = prior aggregate + new delta."""
    r1 = coord.record_telemetry_fold(
        subject="enrichment", counter="timeout_failed", source_table="failure_events",
        grouping={"operation": "enrich", "error_type": "timeout"},
        cutoff_id=100, delta_row_ids=[1, 2, 3], delta_count=3, namespace="ns",
    )
    r2 = coord.record_telemetry_fold(
        subject="enrichment", counter="timeout_failed", source_table="failure_events",
        grouping={"operation": "enrich", "error_type": "timeout"},
        cutoff_id=140, delta_row_ids=[101, 102], delta_count=2, namespace="ns",
    )
    rec1, rec2 = coord.receipts.get(r1["op_id"]), coord.receipts.get(r2["op_id"])
    assert rec1["aggregate_value"] == 3.0
    assert rec2["aggregate_value"] == 5.0  # 3 + 2, not a re-count of the whole table
    assert rec2["previous_receipt_op_id"] == r1["op_id"]
    assert rec2["cutoff_id"] == 140
    # digest chains the prior link
    assert rec2["input_digest"] == chain_digest(rec1["input_digest"], [101, 102], 5.0)
    assert coord.graph_adapter.metrics["ns::enrichment::timeout_failed"]["value"] == 5.0


@pytest.mark.unit
def test_repeated_same_value_writes_commit(coord):
    """An unchanged rewrite must COMMIT, not false-drift into NEEDS_REVIEW (regression).

    A same-value write creates no new version but DOES create a new receipt; the node must be
    repointed at it or the after-state fingerprint mismatches. Same-value folds are the common
    case, and a false drift would fence the key permanently.
    """
    for _ in range(3):
        res = coord.record_run_tally(subject="s", counter="c", value=5.0, namespace="ns")
        assert coord.journal.get(res["op_id"])["state"] == "COMMITTED"
    assert coord.journal.list_by_state("NEEDS_REVIEW") == []
    # The node points at the most recent receipt.
    assert coord.graph_adapter.metrics["ns::s::c"]["receipt_op_id"] == res["op_id"]


@pytest.mark.unit
def test_lww_stale_skip_is_diagnosed_not_silently_false_drifted(coord):
    """If the graph refuses a stale write (LWW), say SO -- don't emit a cryptic fingerprint error.

    The fence makes this unreachable in normal flow, but a silent fall-through would fence the key
    with a misleading "fingerprint mismatch". The operator must see the real cause.
    """
    coord.graph_adapter.record_metric = lambda **kw: {
        "uuid": "m-1", "view_key": "ns::s::c", "stale_skipped": True,
    }
    with pytest.raises(MetricDrift, match="stale replay"):
        coord.record_run_tally(subject="s", counter="c", value=5.0, namespace="ns")

    op = coord.journal.list_by_state("NEEDS_REVIEW")[0]
    assert "LWW stale-skip" in (op["last_error"] or "")


@pytest.mark.unit
def test_telemetry_absolute_recompute(coord):
    """The no-prune path: store the recomputed absolute count, no cross-receipt delta math."""
    r1 = coord.record_telemetry_absolute(
        subject="enrichment", counter="timeout_failed", source_table="failure_events",
        grouping={"operation": "enrich"}, cutoff_id=100, absolute_count=11.0,
        row_ids=list(range(1, 12)), namespace="ns",
    )
    assert coord.receipts.get(r1["op_id"])["aggregate_value"] == 11.0
    assert coord.graph_adapter.metrics["ns::enrichment::timeout_failed"]["value"] == 11.0

    # Unchanged count on the next sweep -> commits, no false drift, no new version.
    r2 = coord.record_telemetry_absolute(
        subject="enrichment", counter="timeout_failed", source_table="failure_events",
        grouping={"operation": "enrich"}, cutoff_id=140, absolute_count=11.0,
        row_ids=list(range(1, 12)), namespace="ns",
    )
    assert coord.journal.get(r2["op_id"])["state"] == "COMMITTED"

    # Higher count -> the metric follows the recomputed absolute (not prior+absolute).
    r3 = coord.record_telemetry_absolute(
        subject="enrichment", counter="timeout_failed", source_table="failure_events",
        grouping={"operation": "enrich"}, cutoff_id=200, absolute_count=15.0,
        row_ids=list(range(1, 16)), namespace="ns",
    )
    assert coord.graph_adapter.metrics["ns::enrichment::timeout_failed"]["value"] == 15.0
    assert coord.receipts.get(r3["op_id"])["previous_receipt_op_id"] == r2["op_id"]


@pytest.mark.unit
def test_rejects_non_allowlisted_source_table(coord):
    with pytest.raises(ValueError, match="not allowlisted"):
        coord.record_telemetry_fold(
            subject="s", counter="c", source_table="; DROP TABLE --", grouping={},
            cutoff_id=1, delta_row_ids=[], delta_count=0,
        )


# ------------------------------------------------------------------ crash recovery (plan E4)


@pytest.mark.unit
def test_crash_after_prepared_before_graph_write_is_replayed(coord):
    """Crash between PREPARED and the graph mutation: reconcile applies it, exactly once."""
    # Simulate: the saga prepared, then the process died before record_metric landed.
    coord.graph_adapter.fail_next = True
    with pytest.raises(RuntimeError):
        coord.record_run_tally(subject="enrichment", counter="stalled", value=4.0, namespace="ns")

    prepared = coord.journal.list_by_state("PREPARED")
    assert len(prepared) == 1  # durable intent survived the crash
    assert coord.graph_adapter.metrics == {}  # nothing landed in the graph

    out = coord.reconcile()
    assert out == {"replayed": 1, "drifted": 0, "failed": 0}
    assert coord.graph_adapter.metrics["ns::enrichment::stalled"]["value"] == 4.0
    assert coord.journal.list_by_state("PREPARED") == []
    assert coord.journal.list_by_state("COMMITTED")[0]["state"] == "COMMITTED"


@pytest.mark.unit
def test_crash_after_graph_write_before_committed_converges(coord):
    """Crash after the mutation but before COMMITTED: replay is a no-op, not a double-write."""
    res = coord.record_run_tally(subject="enrichment", counter="retries", value=7.0, namespace="ns")
    op = res["op_id"]
    writes_before = coord.graph_adapter.write_calls

    # Rewind the journal to PREPARED: the graph write landed, the commit stamp did not.
    with sqlite3.connect(coord.telemetry_db_path) as conn:
        conn.execute(
            "UPDATE graph_operations SET state='PREPARED', committed_at=NULL WHERE op_id=?", (op,)
        )
        conn.commit()

    out = coord.reconcile()
    assert out["replayed"] == 1 and out["drifted"] == 0
    # Value converges, and the row is COMMITTED again.
    assert coord.graph_adapter.metrics["ns::enrichment::retries"]["value"] == 7.0
    assert coord.journal.get(op)["state"] == "COMMITTED"
    # E3 state 2: the graph is ALREADY in the expected after-state, so replay is a true NO-OP --
    # it does not re-issue the write at all (and so cannot fork a node or double-count).
    assert len(coord.graph_adapter.metrics) == 1
    assert coord.graph_adapter.write_calls == writes_before


@pytest.mark.unit
def test_replay_uses_frozen_uuid_and_does_not_fork(coord):
    """The frozen request pins the metric uuid, so replay never mints a competing node."""
    coord.graph_adapter.fail_next = True
    with pytest.raises(RuntimeError):
        coord.record_run_tally(subject="s", counter="c", value=1.0, namespace="ns")
    op = coord.journal.list_by_state("PREPARED")[0]
    frozen_uuid = op["target_uuid"]

    coord.reconcile()
    assert coord.graph_adapter.metrics["ns::s::c"]["uuid"] == frozen_uuid


# ------------------------------------------------------------------ drift (plan E3)


@pytest.mark.unit
def test_drift_marks_needs_review_and_does_not_mutate(coord):
    """Precondition drift -> NEEDS_REVIEW with the graph LEFT ALONE (plan E3 state 3).

    The real record_metric stays wired: if _apply mutated before checking the precondition (the
    bug this pins), the drifted value below would be overwritten and this test would fail.
    """
    res = coord.record_run_tally(subject="s", counter="c", value=2.0, namespace="ns")
    op = res["op_id"]

    # A third party changed the metric out from under us; rewind the journal to force a replay.
    coord.graph_adapter.metrics["ns::s::c"]["value"] = 999.0
    writes_before = coord.graph_adapter.write_calls
    with sqlite3.connect(coord.telemetry_db_path) as conn:
        conn.execute("UPDATE graph_operations SET state='PREPARED' WHERE op_id=?", (op,))
        conn.commit()

    out = coord.reconcile()

    assert out["drifted"] == 1 and out["replayed"] == 0
    assert coord.journal.get(op)["state"] == "NEEDS_REVIEW"
    assert "precondition drift" in (coord.journal.get(op)["last_error"] or "")
    # The graph was NOT touched: value preserved and record_metric never called.
    assert coord.graph_adapter.metrics["ns::s::c"]["value"] == 999.0
    assert coord.graph_adapter.write_calls == writes_before


@pytest.mark.unit
def test_unresolved_op_fences_competing_write(coord):
    """A NEEDS_REVIEW op still fences its key -- a new write must not touch a half-applied node."""
    coord.graph_adapter.fail_next = True
    with pytest.raises(RuntimeError):
        coord.record_run_tally(subject="s", counter="c", value=1.0, namespace="ns")
    op = coord.journal.list_by_state("PREPARED")[0]["op_id"]
    coord.journal.mark_needs_review(op, observed_error="drift")

    from menhir.infrastructure.graph_operations import GraphOperationError

    with pytest.raises(GraphOperationError, match="fences target_key"):
        coord.record_run_tally(subject="s", counter="c", value=2.0, namespace="ns")


@pytest.mark.unit
def test_needs_review_is_not_auto_replayed(coord):
    """Reconcile must not resurrect a NEEDS_REVIEW row (operator-only escape)."""
    coord.graph_adapter.fail_next = True
    with pytest.raises(RuntimeError):
        coord.record_run_tally(subject="s", counter="c", value=1.0, namespace="ns")
    op = coord.journal.list_by_state("PREPARED")[0]["op_id"]
    coord.journal.mark_needs_review(op, observed_error="operator hold")

    out = coord.reconcile()
    assert out == {"replayed": 0, "drifted": 0, "failed": 0}
    assert coord.journal.get(op)["state"] == "NEEDS_REVIEW"
    assert coord.graph_adapter.metrics == {}


# ------------------------------------------------------------------ fingerprint / digest


def _state(**over):
    base = {"uuid": "u", "value": 1.0, "type": "METRIC", "labels": ["Metric"],
            "view_current": True, "receipt_op_id": "op"}
    base.update(over)
    return base


@pytest.mark.unit
def test_fingerprint_is_stable_and_value_sensitive():
    assert state_fingerprint(_state(), view_key="k") == state_fingerprint(_state(), view_key="k")
    assert state_fingerprint(_state(), view_key="k") != state_fingerprint(
        _state(value=2.0), view_key="k"
    )


@pytest.mark.unit
def test_fingerprint_detects_label_and_type_corruption():
    """A Metric relabelled :Entity or stripped of type='METRIC' must NOT fingerprint as correct."""
    good = state_fingerprint(_state(), view_key="k")
    assert state_fingerprint(_state(labels=["Entity"]), view_key="k") != good
    assert state_fingerprint(_state(type="SEMANTIC"), view_key="k") != good
    assert state_fingerprint(_state(view_current=False), view_key="k") != good
    assert state_fingerprint(_state(receipt_op_id="other"), view_key="k") != good


@pytest.mark.unit
def test_absent_state_has_its_own_fingerprint():
    assert state_fingerprint(None, view_key="k") != state_fingerprint(None, view_key="other")
    assert state_fingerprint(None, view_key="k") != state_fingerprint(_state(), view_key="k")


@pytest.mark.unit
def test_fingerprint_is_current_set_cardinality_sensitive():
    """A duplicate-current graph must NOT fingerprint like a healthy single-current one.

    Plan E Phase 2: the expected after-state asserts exactly one current version. Folding
    current_count into the fingerprint is what lets the saga reject a two-current graph
    (route to NEEDS_REVIEW) instead of reading the newest of the pair as a clean commit.
    A state without the field defaults to a single current, so existing callers are unchanged.
    """
    single = state_fingerprint(_state(), view_key="k")
    assert state_fingerprint(_state(current_count=1), view_key="k") == single
    assert state_fingerprint(_state(current_count=2), view_key="k") != single


# ------------------------------------------------------------------ fail-closed / chain integrity


@pytest.mark.unit
def test_missing_precondition_fails_closed(coord):
    """A request with no frozen precondition must NOT be applied -- refusing beats guessing."""
    coord.graph_adapter.fail_next = True
    with pytest.raises(RuntimeError):
        coord.record_run_tally(subject="s", counter="c", value=1.0, namespace="ns")
    op = coord.journal.list_by_state("PREPARED")[0]["op_id"]

    # Strip the frozen precondition (simulates a legacy/tampered request row).
    import json as _json
    row = coord.journal.get(op)
    req = _json.loads(row["request_json"])
    req.pop("expected_before_sha256")
    with sqlite3.connect(coord.telemetry_db_path) as conn:
        conn.execute(
            "UPDATE graph_operations SET request_json=? WHERE op_id=?", (_json.dumps(req), op)
        )
        conn.commit()

    writes_before = coord.graph_adapter.write_calls
    out = coord.reconcile()

    assert out["drifted"] == 1 and out["replayed"] == 0
    assert coord.journal.get(op)["state"] == "NEEDS_REVIEW"
    assert coord.graph_adapter.write_calls == writes_before  # never mutated
    assert coord.graph_adapter.metrics == {}


@pytest.mark.unit
def test_chain_head_ignores_receipts_whose_graph_write_never_landed(coord):
    """An orphan receipt (op stuck PREPARED/NEEDS_REVIEW) must NOT become the accumulator head.

    A receipt is committed at PREPARE, but its graph write can still fail. Chaining the next fold
    onto that orphan would anchor the aggregate to a value the graph never reached.
    """
    # A committed fold: aggregate 3.
    good = coord.record_telemetry_fold(
        subject="e", counter="c", source_table="failure_events", grouping={},
        cutoff_id=10, delta_row_ids=[1, 2, 3], delta_count=3, namespace="ns",
    )
    assert coord.receipts.get(good["op_id"])["aggregate_value"] == 3.0

    # A fold whose graph write fails: its receipt IS committed, but the op stays PREPARED.
    coord.graph_adapter.fail_next = True
    with pytest.raises(RuntimeError):
        coord.record_telemetry_fold(
            subject="e", counter="c", source_table="failure_events", grouping={},
            cutoff_id=20, delta_row_ids=[11, 12], delta_count=2, namespace="ns",
        )
    orphan = coord.journal.list_by_state("PREPARED")[0]["op_id"]
    assert coord.receipts.get(orphan) is not None  # the orphan receipt exists...

    # ...but the chain head is still the COMMITTED one, so the lineage is not poisoned.
    head = coord.receipts.latest_for_key("ns::e::c")
    assert head["op_id"] == good["op_id"]
    assert head["aggregate_value"] == 3.0
    # Without committed_only, the orphan would be the head -- prove that is what we're excluding.
    assert coord.receipts.latest_for_key("ns::e::c", committed_only=False)["op_id"] == orphan


@pytest.mark.unit
def test_chain_digest_is_order_insensitive_to_row_id_input():
    """Canonicalisation sorts row ids, so the digest is stable regardless of fold row order."""
    assert chain_digest("prev", [3, 1, 2], 6.0) == chain_digest("prev", [1, 2, 3], 6.0)
    assert chain_digest("prev", [1, 2, 3], 6.0) != chain_digest("other", [1, 2, 3], 6.0)
