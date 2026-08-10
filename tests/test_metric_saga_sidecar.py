"""Unit tests for the Metric saga sidecar foundations (Plan 2, implementation step 1).

Covers the three SQLite stores that make Metric writes and the migration reversible:
graph_operations journal, metric_receipts (chained accumulators), and migration_batches.
All use an explicit temp db_path -- no telemetry-DB contact.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from menhir.infrastructure.graph_operations import GraphOperationError, GraphOperationsJournal
from menhir.infrastructure.metric_receipts import MetricReceiptError, MetricReceiptStore
from menhir.infrastructure.migration_batches import MigrationBatchError, MigrationBatchStore


# --------------------------------------------------------------------- graph_operations


@pytest.fixture
def journal(tmp_path):
    return GraphOperationsJournal(db_path=tmp_path / "saga.db")


@pytest.mark.unit
def test_prepare_then_commit(journal):
    op = journal.prepare(
        operation_kind="METRIC_WRITE", request_json="{}", target_key="k1",
        expected_after_sha256="abc",
    )
    row = journal.get(op)
    assert row["state"] == "PREPARED" and row["committed_at"] is None
    journal.mark_committed(op)
    row = journal.get(op)
    assert row["state"] == "COMMITTED"
    assert row["committed_at"] is not None
    # Fingerprint is frozen at PREPARE and unchanged by commit.
    assert row["expected_after_sha256"] == "abc"


@pytest.mark.unit
def test_expected_after_sha256_frozen_at_prepare(journal):
    """The postcondition fingerprint is immutable after PREPARE (plan E1)."""
    op = journal.prepare(
        operation_kind="METRIC_WRITE", request_json="{}", target_key="k",
        expected_after_sha256="frozen",
    )
    journal.mark_committed(op)  # no way to pass a different fingerprint
    assert journal.get(op)["expected_after_sha256"] == "frozen"


@pytest.mark.unit
def test_request_json_immutable_across_transitions(journal):
    """No transition may rewrite request_json (immutable-after-PREPARED, plan E1)."""
    op = journal.prepare(
        operation_kind="METRIC_MIGRATE", request_json='{"uuid":"u1","value":5}',
        batch_id="b", target_uuid="u1",
    )
    before = journal.get(op)["request_json"]
    journal.mark_needs_review(op, observed_error="drift")
    journal.clear_needs_review(op, to_state="PREPARED")
    journal.mark_committed(op)
    journal.mark_reversed(op)
    assert journal.get(op)["request_json"] == before


@pytest.mark.unit
def test_unknown_operation_kind_rejected(journal):
    with pytest.raises(GraphOperationError):
        journal.prepare(operation_kind="NONSENSE", request_json="{}")


@pytest.mark.unit
def test_prepared_write_fences_target_key(journal):
    """Only one in-flight PREPARED routine write may exist per target_key."""
    journal.prepare(operation_kind="METRIC_WRITE", request_json="{}", target_key="dup")
    with pytest.raises(GraphOperationError, match="fences target_key"):
        journal.prepare(operation_kind="METRIC_WRITE", request_json="{}", target_key="dup")


@pytest.mark.unit
def test_fence_released_after_commit(journal):
    op = journal.prepare(operation_kind="METRIC_WRITE", request_json="{}", target_key="k")
    journal.mark_committed(op)
    # Once the first is COMMITTED it no longer fences -- a fresh write may prepare.
    op2 = journal.prepare(operation_kind="METRIC_WRITE", request_json="{}", target_key="k")
    assert journal.get(op2)["state"] == "PREPARED"


@pytest.mark.unit
def test_batch_target_uniqueness(journal):
    journal.prepare(
        operation_kind="METRIC_MIGRATE", request_json="{}", batch_id="b1", target_uuid="u1"
    )
    with pytest.raises(GraphOperationError, match="already enqueued"):
        journal.prepare(
            operation_kind="METRIC_MIGRATE", request_json="{}", batch_id="b1", target_uuid="u1"
        )


@pytest.mark.unit
def test_illegal_transition_rejected(journal):
    op = journal.prepare(operation_kind="METRIC_WRITE", request_json="{}", target_key="k")
    journal.mark_committed(op)
    # COMMITTED cannot go back to COMMITTED-from-PREPARED semantics via mark_committed twice
    journal.mark_committed(op)  # idempotent no-op
    # A committed row cannot be marked reversed's precondition violated etc.
    with pytest.raises(GraphOperationError, match="illegal transition"):
        journal.clear_needs_review(op, to_state="PREPARED")


@pytest.mark.unit
def test_needs_review_only_cleared_by_operator(journal):
    op = journal.prepare(operation_kind="METRIC_MIGRATE", request_json="{}", batch_id="b",
                         target_uuid="u")
    journal.mark_needs_review(op, observed_error="drift")
    row = journal.get(op)
    assert row["state"] == "NEEDS_REVIEW" and row["last_error"] == "drift"
    # Explicit operator escape is allowed; background transitions are not expressible.
    journal.clear_needs_review(op, to_state="PREPARED")
    assert journal.get(op)["state"] == "PREPARED"


@pytest.mark.unit
def test_reverse_flow(journal):
    op = journal.prepare(operation_kind="METRIC_MIGRATE", request_json="{}", batch_id="b",
                         target_uuid="u")
    journal.mark_committed(op)
    journal.mark_reversed(op)
    assert journal.get(op)["state"] == "REVERSED"


@pytest.mark.unit
def test_record_attempt_increments(journal):
    op = journal.prepare(operation_kind="METRIC_WRITE", request_json="{}", target_key="k")
    journal.record_attempt(op, error="boom")
    journal.record_attempt(op, error="boom2")
    row = journal.get(op)
    assert row["attempt_count"] == 2 and row["last_error"] == "boom2"


@pytest.mark.unit
def test_list_by_state_and_batch(journal):
    a = journal.prepare(operation_kind="METRIC_MIGRATE", request_json="{}", batch_id="b",
                        target_uuid="ua")
    journal.prepare(operation_kind="METRIC_MIGRATE", request_json="{}", batch_id="b",
                    target_uuid="ub")
    assert len(journal.list_by_state("PREPARED")) == 2
    assert {r["op_id"] for r in journal.list_by_batch("b")} >= {a}


# ------------------------------------------------- per-participant fence (invariant 14)


def _merge_req(survivor: str, absorbed: str) -> str:
    return json.dumps({"survivor_uuid": survivor, "absorbed_uuid": absorbed})


def _pair_key(survivor: str, absorbed: str) -> str:
    return "::".join(sorted([survivor, absorbed]))


@pytest.mark.unit
def test_participant_fence_blocks_merge_sharing_a_node(journal):
    """A+B PREPARED, then an unrelated B+C merge is fenced on the shared node B, even though the
    two pair keys differ (the pair-key fence alone would miss this)."""
    journal.prepare(
        operation_kind="ENTITY_MERGE", request_json=_merge_req("A", "B"),
        target_uuid="B", target_key=_pair_key("A", "B"),
    )
    with pytest.raises(GraphOperationError, match="already fenced"):
        journal.prepare(
            operation_kind="ENTITY_MERGE", request_json=_merge_req("B", "C"),
            target_uuid="C", target_key=_pair_key("B", "C"),
        )


@pytest.mark.unit
def test_participant_fence_released_after_commit(journal):
    op = journal.prepare(
        operation_kind="ENTITY_MERGE", request_json=_merge_req("A", "B"),
        target_uuid="B", target_key=_pair_key("A", "B"),
    )
    journal.mark_committed(op)
    # B is free again once the A+B merge COMMITTED.
    op2 = journal.prepare(
        operation_kind="ENTITY_MERGE", request_json=_merge_req("B", "C"),
        target_uuid="C", target_key=_pair_key("B", "C"),
    )
    assert journal.get(op2)["state"] == "PREPARED"


@pytest.mark.unit
def test_delete_fenced_while_merge_unresolved(journal):
    """A delete of B is blocked while an ENTITY_MERGE A+B is unresolved (cross-kind fence)."""
    journal.prepare(
        operation_kind="ENTITY_MERGE", request_json=_merge_req("A", "B"),
        target_uuid="B", target_key=_pair_key("A", "B"),
    )
    with pytest.raises(GraphOperationError, match="already fenced"):
        journal.prepare(
            operation_kind="ENTITY_DELETE",
            request_json=json.dumps({"targets": ["B"]}),
            target_key=None,
        )


@pytest.mark.unit
def test_needs_review_keeps_participant_fence(journal):
    op = journal.prepare(
        operation_kind="ENTITY_MERGE", request_json=_merge_req("A", "B"),
        target_uuid="B", target_key=_pair_key("A", "B"),
    )
    journal.mark_needs_review(op, observed_error="drift")
    with pytest.raises(GraphOperationError, match="already fenced"):
        journal.prepare(
            operation_kind="ENTITY_DELETE",
            request_json=json.dumps({"targets": ["B"]}),
            target_key=None,
        )


@pytest.mark.unit
def test_failed_releases_participant_fence(journal):
    op = journal.prepare(
        operation_kind="ENTITY_MERGE", request_json=_merge_req("A", "B"),
        target_uuid="B", target_key=_pair_key("A", "B"),
    )
    journal.mark_failed(op, reason="repository abstained")
    # FAILED is a terminal, non-quarantine state: the participant is released.
    op2 = journal.prepare(
        operation_kind="ENTITY_DELETE",
        request_json=json.dumps({"targets": ["B"]}),
        target_key=None,
    )
    assert journal.get(op2)["state"] == "PREPARED"


@pytest.mark.unit
def test_metric_kinds_take_no_participant_lock(tmp_path):
    """METRIC_* kinds fence on target_key only and never populate graph_operation_locks."""
    db = tmp_path / "saga.db"
    journal = GraphOperationsJournal(db_path=db)
    journal.prepare(operation_kind="METRIC_WRITE", request_json="{}", target_key="k")
    with sqlite3.connect(db) as conn:
        count = conn.execute("SELECT COUNT(*) FROM graph_operation_locks").fetchone()[0]
    assert count == 0


@pytest.mark.unit
def test_backfill_activates_fence_for_preexisting_unresolved_row(tmp_path):
    """A row written before the lock table existed (no lock rows) is re-fenced on the next
    _ensure_ready via backfill, so its participants are protected going forward."""
    db = tmp_path / "saga.db"
    GraphOperationsJournal(db_path=db)._ensure_ready()  # create schema

    # Simulate a pre-migration unresolved merge: a journal row with NO participant locks.
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO graph_operations "
            "(op_id, operation_kind, target_uuid, target_key, request_json, state, "
            "attempt_count, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 'PREPARED', 0, ?, ?)",
            ("legacyop", "ENTITY_MERGE", "B", _pair_key("A", "B"), _merge_req("A", "B"),
             "2026-07-13T00:00:00+00:00", "2026-07-13T00:00:00+00:00"),
        )
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM graph_operation_locks").fetchone()[0] == 0

    # A fresh instance runs _ensure_ready -> backfill materializes the missing locks.
    fresh = GraphOperationsJournal(db_path=db)
    fresh._ensure_ready()
    with sqlite3.connect(db) as conn:
        locked = {r[0] for r in conn.execute("SELECT entity_uuid FROM graph_operation_locks")}
    assert locked == {"A", "B"}

    # The fence is now active: a delete of B abstains.
    with pytest.raises(GraphOperationError, match="already fenced"):
        fresh.prepare(
            operation_kind="ENTITY_DELETE",
            request_json=json.dumps({"targets": ["B"]}),
            target_key=None,
        )


# --------------------------------------------------------------------- metric_receipts


@pytest.fixture
def receipts(tmp_path):
    return MetricReceiptStore(db_path=tmp_path / "saga.db")


@pytest.mark.unit
def test_receipt_append_and_get(receipts):
    receipts.append(
        op_id="op1", receipt_kind="TELEMETRY_FOLD", metric_uuid="m1", view_key="k",
        aggregate_value=11.0, source_table="failure_events", cutoff_id=100,
        source_row_count=11, input_digest="d1",
    )
    row = receipts.get("op1")
    assert row["receipt_kind"] == "TELEMETRY_FOLD"
    assert row["aggregate_value"] == 11.0
    assert row["cutoff_id"] == 100


@pytest.mark.unit
def test_receipt_is_append_only(receipts):
    receipts.append(op_id="op1", receipt_kind="RUN_TALLY", metric_uuid="m", view_key="k",
                    aggregate_value=1.0)
    with pytest.raises(MetricReceiptError, match="append-only"):
        receipts.append(op_id="op1", receipt_kind="RUN_TALLY", metric_uuid="m", view_key="k",
                        aggregate_value=2.0)


@pytest.mark.unit
def test_unknown_receipt_kind_rejected(receipts):
    with pytest.raises(MetricReceiptError):
        receipts.append(op_id="op1", receipt_kind="WHAT", metric_uuid="m", view_key="k",
                        aggregate_value=1.0)


@pytest.mark.unit
def test_accumulator_chain_ordering(receipts):
    receipts.append(op_id="op1", receipt_kind="TELEMETRY_FOLD", metric_uuid="m", view_key="k",
                    aggregate_value=5.0, cutoff_id=50, input_digest="d1")
    receipts.append(op_id="op2", receipt_kind="TELEMETRY_FOLD", metric_uuid="m", view_key="k",
                    aggregate_value=8.0, cutoff_id=80, previous_receipt_op_id="op1",
                    input_digest="d2")
    chain = receipts.chain_for_key("k")
    assert [r["op_id"] for r in chain] == ["op1", "op2"]
    # committed_only=False: this store-level test has no graph_operations rows to join against.
    # The committed-only chain head (the default) is covered in test_metric_write_coordinator.
    latest = receipts.latest_for_key("k", committed_only=False)
    assert latest["op_id"] == "op2" and latest["previous_receipt_op_id"] == "op1"


@pytest.mark.unit
def test_chain_order_deterministic_on_timestamp_tie(receipts, monkeypatch):
    """Two receipts written in the same microsecond still chain by insertion (rowid)."""
    import menhir.infrastructure.metric_receipts as mod

    monkeypatch.setattr(mod, "_utc_now_iso", lambda: "2026-07-13T10:00:00.000000+00:00")
    receipts.append(op_id="first", receipt_kind="TELEMETRY_FOLD", metric_uuid="m", view_key="k",
                    aggregate_value=5.0)
    receipts.append(op_id="second", receipt_kind="TELEMETRY_FOLD", metric_uuid="m", view_key="k",
                    aggregate_value=8.0, previous_receipt_op_id="first")
    assert [r["op_id"] for r in receipts.chain_for_key("k")] == ["first", "second"]
    assert receipts.latest_for_key("k", committed_only=False)["op_id"] == "second"


# --------------------------------------------------------------------- migration_batches


@pytest.fixture
def batches(tmp_path):
    return MigrationBatchStore(db_path=tmp_path / "saga.db")


@pytest.mark.unit
def test_dry_run_then_approve_then_apply(batches):
    bid = batches.create_dry_run(
        classifier_version="v1", manifest_sha256="sha123", manifest_json="[]", target_count=313,
    )
    assert batches.get(bid)["state"] == "DRY_RUN"
    batches.approve(bid, manifest_sha256="sha123")
    assert batches.get(bid)["state"] == "APPROVED"
    batches.mark_applied(bid, manifest_sha256="sha123")
    row = batches.get(bid)
    assert row["state"] == "APPLIED"
    assert row["approved_at"] is not None and row["applied_at"] is not None


@pytest.mark.unit
def test_approve_requires_exact_sha(batches):
    bid = batches.create_dry_run(
        classifier_version="v1", manifest_sha256="right", manifest_json="[]", target_count=1,
    )
    with pytest.raises(MigrationBatchError, match="SHA mismatch"):
        batches.approve(bid, manifest_sha256="wrong")
    assert batches.get(bid)["state"] == "DRY_RUN"


@pytest.mark.unit
def test_apply_requires_approved_state(batches):
    bid = batches.create_dry_run(
        classifier_version="v1", manifest_sha256="s", manifest_json="[]", target_count=1,
    )
    with pytest.raises(MigrationBatchError, match="illegal batch transition"):
        batches.mark_applied(bid, manifest_sha256="s")


# --------------------------------------------------- cross-store atomicity (plan E2)


@pytest.mark.unit
def test_operation_and_receipt_commit_atomically(tmp_path):
    """A shared connection commits the operation row and its receipt in ONE transaction."""
    db = tmp_path / "saga.db"
    journal = GraphOperationsJournal(db_path=db)
    receipts = MetricReceiptStore(db_path=db)
    journal._ensure_ready()
    receipts._ensure_ready()

    with sqlite3.connect(db) as conn:
        op = journal.prepare(
            operation_kind="METRIC_WRITE", request_json="{}", target_key="k", conn=conn
        )
        receipts.append(
            op_id=op, receipt_kind="TELEMETRY_FOLD", metric_uuid="m", view_key="k",
            aggregate_value=1.0, conn=conn,
        )
        conn.commit()

    assert journal.get(op)["state"] == "PREPARED"
    assert receipts.get(op) is not None


@pytest.mark.unit
def test_operation_and_receipt_rollback_together(tmp_path):
    """If the bundled transaction rolls back, NEITHER the op nor the receipt persists."""
    db = tmp_path / "saga.db"
    journal = GraphOperationsJournal(db_path=db)
    receipts = MetricReceiptStore(db_path=db)
    journal._ensure_ready()
    receipts._ensure_ready()

    conn = sqlite3.connect(db)
    try:
        op = journal.prepare(
            operation_kind="METRIC_WRITE", request_json="{}", target_key="k", conn=conn
        )
        receipts.append(
            op_id=op, receipt_kind="TELEMETRY_FOLD", metric_uuid="m", view_key="k",
            aggregate_value=1.0, conn=conn,
        )
        conn.rollback()  # simulate a failure before commit
    finally:
        conn.close()

    assert journal.get(op) is None
    assert receipts.get(op) is None
