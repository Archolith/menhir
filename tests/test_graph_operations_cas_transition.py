"""CF-271: ``_transition`` is a compare-and-set (offline, real SQLite journal in a tmp file)."""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from menhir.infrastructure.graph_operations import GraphOperationsJournal
from menhir.infrastructure.graph_operations_model import GraphOperationError
from menhir.infrastructure.graph_operations_transitions import StaleTransitionError


def _prepare(journal, *, owner="owner-A"):
    return journal.prepare(
        operation_kind="ENTITY_MERGE",
        request_json=json.dumps({"absorbed_uuid": "abs", "survivor_uuid": "sur"}),
        owner_token=owner,
    )


def _row(journal, op_id):
    with sqlite3.connect(journal.db_path) as conn:
        return conn.execute(
            "SELECT state, owner_token, last_error FROM graph_operations WHERE op_id = ?", (op_id,)
        ).fetchone()


def _lock_count(journal, op_id):
    with sqlite3.connect(journal.db_path) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM graph_operation_locks WHERE op_id = ?", (op_id,)
        ).fetchone()[0]


def _take_over(journal, op_id, new_owner):
    """Reconciler B's claim: the observable effect of a successful takeover."""
    with sqlite3.connect(journal.db_path) as conn:
        conn.execute(
            "UPDATE graph_operations SET owner_token = ? WHERE op_id = ?", (new_owner, op_id)
        )


@pytest.mark.unit
def test_lost_claim_writer_cannot_fail_a_committed_row(tmp_path):
    journal = GraphOperationsJournal(db_path=tmp_path / "ops.db")
    op_id = _prepare(journal, owner="owner-A")
    assert _lock_count(journal, op_id) == 2
    # A's lease expires; reconciler B takes over and commits.
    _take_over(journal, op_id, "owner-B")
    journal.mark_committed(op_id, owner_token="owner-B")
    assert _row(journal, op_id)[0] == "COMMITTED"
    locks_after_commit = _lock_count(journal, op_id)

    # A resumes and tries FAILED: 0 rows, reported, nothing changes.
    with pytest.raises(StaleTransitionError):
        journal.mark_failed(op_id, reason="late abstain", owner_token="owner-A")
    with pytest.raises(StaleTransitionError):
        journal.mark_failed(op_id, reason="late abstain")  # even without a token
    state, _owner, last_error = _row(journal, op_id)
    assert state == "COMMITTED"
    assert last_error is None
    assert _lock_count(journal, op_id) == locks_after_commit


@pytest.mark.unit
def test_stale_failed_does_not_release_locks_of_a_prepared_row_it_no_longer_owns(tmp_path):
    journal = GraphOperationsJournal(db_path=tmp_path / "ops.db")
    op_id = _prepare(journal, owner="owner-A")
    _take_over(journal, op_id, "owner-B")
    with pytest.raises(StaleTransitionError):
        journal.mark_failed(op_id, reason="stale", owner_token="owner-A")
    state, owner, _ = _row(journal, op_id)
    assert (state, owner) == ("PREPARED", "owner-B")
    assert _lock_count(journal, op_id) == 2


@pytest.mark.unit
def test_wrong_owner_is_refused_and_right_owner_commits(tmp_path):
    journal = GraphOperationsJournal(db_path=tmp_path / "ops.db")
    op_id = _prepare(journal, owner="owner-A")
    with pytest.raises(StaleTransitionError):
        journal.mark_committed(op_id, owner_token="someone-else")
    assert _row(journal, op_id)[0] == "PREPARED"
    assert _lock_count(journal, op_id) == 2
    journal.mark_committed(op_id, owner_token="owner-A")
    state, owner, _ = _row(journal, op_id)
    assert state == "COMMITTED"
    assert owner is None  # claim retired on leaving PREPARED
    assert _lock_count(journal, op_id) == 0


@pytest.mark.unit
def test_normal_paths_still_transition(tmp_path):
    journal = GraphOperationsJournal(db_path=tmp_path / "ops.db")
    committed = _prepare(journal)
    journal.mark_committed(committed)
    journal.mark_committed(committed)  # idempotent re-commit stays a no-op
    assert _row(journal, committed)[0] == "COMMITTED"
    journal.mark_reversed(committed)
    assert _row(journal, committed)[0] == "REVERSED"

    failed = journal.prepare(
        operation_kind="ENTITY_MERGE",
        request_json=json.dumps({"absorbed_uuid": "x", "survivor_uuid": "y"}),
    )
    journal.mark_failed(failed, reason="abstained")
    assert _row(journal, failed)[0] == "FAILED"
    assert _lock_count(journal, failed) == 0

    review = journal.prepare(
        operation_kind="ENTITY_MERGE",
        request_json=json.dumps({"absorbed_uuid": "p", "survivor_uuid": "q"}),
    )
    journal.mark_needs_review(review, observed_error="drift")
    assert _row(journal, review)[0] == "NEEDS_REVIEW"
    assert _lock_count(journal, review) == 2  # quarantine keeps the fence


@pytest.mark.unit
def test_unknown_op_and_illegal_transition_keep_graph_operation_error(tmp_path):
    journal = GraphOperationsJournal(db_path=tmp_path / "ops.db")
    with pytest.raises(GraphOperationError):
        journal.mark_committed("missing")
    op_id = _prepare(journal)
    with pytest.raises(GraphOperationError):  # PREPARED -> REVERSED is illegal
        journal.mark_reversed(op_id)
    assert issubclass(StaleTransitionError, GraphOperationError)


@pytest.mark.unit
def test_caller_stops_on_lost_cas_and_does_not_retry(tmp_path):
    """A stale writer's mark_failed in MergeCoordinator surfaces the error once, no retry."""
    from menhir.services.merge_coordinator import MergeCoordinator

    journal = MagicMock()
    journal.mark_failed.side_effect = StaleTransitionError("lost")
    coord = MergeCoordinator.__new__(MergeCoordinator)
    coord.journal = journal
    coord.graph_adapter = MagicMock()
    coord.graph_adapter.merge_entity.return_value = {"merged": 0, "reason": "GATE"}
    coord._classify_replay = lambda request: (
        "OTHER",
        {"expected_before": "fp", "expected_after": "fp2"},
    )
    coord.graph_adapter.fetch_merge_state.return_value = SimpleNamespace()
    import menhir.services.merge_coordinator as mc

    original = mc.merge_state_fingerprint
    mc.merge_state_fingerprint = lambda *_a, **_k: "fp"
    try:
        request = {
            "op_id": "op1", "survivor_uuid": "s", "absorbed_uuid": "a",
            "similarity": 0.9, "expected_fact_uuids": [],
        }
        with pytest.raises(StaleTransitionError):
            coord._apply_owned(request)
    finally:
        mc.merge_state_fingerprint = original
    assert journal.mark_failed.call_count == 1
    journal.mark_committed.assert_not_called()
    journal.mark_needs_review.assert_not_called()
