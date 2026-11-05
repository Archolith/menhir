"""CF-271 second-pass review F-1 and F-2.

F-1: the commit-refused failure event must keep the op's identity when it reaches the real
telemetry store (non-episode events are redacted to an allowlist).

F-2: writer A's claim is taken over by B; B commits; A's statement lands and A calls
mark_committed. The row is already COMMITTED, but by B, so A must get StaleTransitionError (and the
F2 record), not the idempotent success that told A its own commit happened.
"""

from __future__ import annotations

import functools
import json
import sqlite3

import pytest

from menhir.infrastructure.graph_operations import GraphOperationsJournal
from menhir.infrastructure.graph_operations_transitions import StaleTransitionError
from menhir.infrastructure.telemetry import recorders
from menhir.infrastructure.telemetry.store import McpTelemetryStore

pytestmark = pytest.mark.unit


def _prepare(journal: GraphOperationsJournal, owner: str) -> str:
    return journal.prepare(
        operation_kind="ENTITY_MERGE",
        request_json=json.dumps({"absorbed_uuid": "abs", "survivor_uuid": "sur"}),
        owner_token=owner,
    )


def _take_over(journal: GraphOperationsJournal, op_id: str, owner: str) -> None:
    with sqlite3.connect(journal.db_path) as conn:
        conn.execute("UPDATE graph_operations SET owner_token = ? WHERE op_id = ?", (owner, op_id))


def _row(journal: GraphOperationsJournal, op_id: str) -> tuple[str, str | None]:
    with sqlite3.connect(journal.db_path) as conn:
        return conn.execute(
            "SELECT state, resolved_by_token FROM graph_operations WHERE op_id = ?", (op_id,)
        ).fetchone()


@pytest.fixture
def store(tmp_path, monkeypatch) -> McpTelemetryStore:
    real = McpTelemetryStore(db_path=tmp_path / "telemetry.db")
    real._ensure_ready()  # failure_events exists even when nothing is recorded
    monkeypatch.setattr(
        recorders, "record_failure_event",
        functools.partial(recorders.record_failure_event, store=real),
    )
    return real


def _commit_events(store: McpTelemetryStore) -> list[dict]:
    with sqlite3.connect(store.db_path) as conn:
        rows = conn.execute(
            "SELECT failure_stage, details_json FROM failure_events WHERE operation = ?",
            ("graph_operation_commit",),
        ).fetchall()
    return [{"failure_stage": stage, **json.loads(details)} for stage, details in rows]


def test_f1_commit_refused_event_keeps_op_identity_in_the_store(tmp_path, store):
    journal = GraphOperationsJournal(db_path=tmp_path / "ops.db")
    op_id = _prepare(journal, "owner-A")
    _take_over(journal, op_id, "owner-B")
    journal.mark_failed(op_id, reason="abstained: before-state", owner_token="owner-B")

    with pytest.raises(StaleTransitionError):
        journal.mark_committed(op_id, owner_token="owner-A")

    assert _commit_events(store) == [{
        "failure_stage": "commit_refused_after_graph_work",
        "op_id": op_id,
        "operation_kind": "ENTITY_MERGE",
        "journal_state": "FAILED",
    }]


def test_f2_stale_commit_after_new_owner_committed_is_refused(tmp_path, store):
    journal = GraphOperationsJournal(db_path=tmp_path / "ops.db")
    op_id = _prepare(journal, "owner-A")
    _take_over(journal, op_id, "owner-B")
    journal.mark_committed(op_id, owner_token="owner-B")

    with pytest.raises(StaleTransitionError, match="another owner"):
        journal.mark_committed(op_id, owner_token="owner-A")

    assert _row(journal, op_id) == ("COMMITTED", "owner-B")
    assert [e["journal_state"] for e in _commit_events(store)] == ["COMMITTED"]


def test_f2_stale_commit_log_names_the_other_owner(tmp_path, store, caplog):
    journal = GraphOperationsJournal(db_path=tmp_path / "ops.db")
    op_id = _prepare(journal, "owner-A")
    _take_over(journal, op_id, "owner-B")
    journal.mark_committed(op_id, owner_token="owner-B")

    with caplog.at_level("ERROR"), pytest.raises(StaleTransitionError):
        journal.mark_committed(op_id, owner_token="owner-A")
    assert any("another owner already committed" in r.getMessage() for r in caplog.records)


def test_f2_guard_is_commit_only(tmp_path, store):
    """Fix review FR-9: an already-FAILED row stays idempotent for a different token."""
    journal = GraphOperationsJournal(db_path=tmp_path / "ops.db")
    op_id = _prepare(journal, "owner-A")
    _take_over(journal, op_id, "owner-B")
    journal.mark_failed(op_id, reason="reconciler", owner_token="owner-B")

    journal.mark_failed(op_id, reason="stale writer", owner_token="owner-A")
    assert _row(journal, op_id) == ("FAILED", "owner-B")


def test_f2_tokenless_caller_on_a_row_another_owner_committed_stays_idempotent(tmp_path, store):
    """Fix review FR-9: the guard applies only to a caller that presents a token."""
    journal = GraphOperationsJournal(db_path=tmp_path / "ops.db")
    op_id = _prepare(journal, "owner-A")
    _take_over(journal, op_id, "owner-B")
    journal.mark_committed(op_id, owner_token="owner-B")

    journal.mark_committed(op_id)
    assert _row(journal, op_id) == ("COMMITTED", "owner-B")
    assert _commit_events(store) == []


def test_f2_same_owner_repeat_commit_stays_idempotent(tmp_path, store):
    journal = GraphOperationsJournal(db_path=tmp_path / "ops.db")
    op_id = _prepare(journal, "owner-A")
    journal.mark_committed(op_id, owner_token="owner-A")
    journal.mark_committed(op_id, owner_token="owner-A")

    assert _row(journal, op_id) == ("COMMITTED", "owner-A")
    assert _commit_events(store) == []


def test_f2_tokenless_commit_has_no_owner_evidence_and_stays_idempotent(tmp_path, store):
    journal = GraphOperationsJournal(db_path=tmp_path / "ops.db")
    op_id = _prepare(journal, "owner-A")
    journal.mark_committed(op_id)  # no token: nothing recorded about who committed

    journal.mark_committed(op_id, owner_token="owner-A")
    assert _row(journal, op_id) == ("COMMITTED", None)
    assert _commit_events(store) == []


def test_f2_operator_clear_to_committed_keeps_the_reviewer_token(tmp_path, store):
    journal = GraphOperationsJournal(db_path=tmp_path / "ops.db")
    op_id = _prepare(journal, "owner-A")
    _take_over(journal, op_id, "owner-B")
    journal.mark_needs_review(op_id, observed_error="drift", owner_token="owner-B")
    journal.clear_needs_review(op_id, to_state="COMMITTED")  # operator, tokenless

    with pytest.raises(StaleTransitionError):
        journal.mark_committed(op_id, owner_token="owner-A")
    assert _row(journal, op_id) == ("COMMITTED", "owner-B")


def test_pre_existing_journal_gains_the_column(tmp_path):
    db = tmp_path / "ops.db"
    old_columns = (
        "op_id TEXT PRIMARY KEY, batch_id TEXT, operation_kind TEXT NOT NULL, target_uuid TEXT, "
        "target_key TEXT, request_json TEXT NOT NULL, before_snapshot_json TEXT, "
        "expected_after_sha256 TEXT, state TEXT NOT NULL, attempt_count INTEGER NOT NULL DEFAULT 0, "
        "last_error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, committed_at TEXT, "
        "reverses_op_id TEXT, owner_token TEXT, owner_heartbeat_at TEXT, owner_lease_expires_at TEXT, "
        "owner_death_attested_by TEXT, owner_death_attested_at TEXT, owner_death_attested_for_token TEXT"
    )
    with sqlite3.connect(db) as conn:
        conn.execute(f"CREATE TABLE graph_operations ({old_columns})")

    journal = GraphOperationsJournal(db_path=db)
    op_id = _prepare(journal, "owner-A")
    journal.mark_committed(op_id, owner_token="owner-A")
    assert _row(journal, op_id) == ("COMMITTED", "owner-A")
