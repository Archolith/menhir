"""CF-271 F2: a writer whose commit loses after its graph work leaves a durable record.

Trace: writer A's statement is in flight; reconciler B (false death verdict) claims, sees the
before-state, marks FAILED. A's statement lands, then A's mark_committed is refused. Only A knows
the graph moved, so the refusal itself must persist a failure event -- the journal state is left
alone (no new transition) and the StaleTransitionError still reaches the caller.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from menhir.infrastructure.graph_operations import GraphOperationsJournal
from menhir.infrastructure.graph_operations_transitions import StaleTransitionError
from menhir.infrastructure.telemetry import recorders


def _prepare(journal: GraphOperationsJournal, owner: str) -> str:
    return journal.prepare(
        operation_kind="ENTITY_MERGE",
        request_json=json.dumps({"absorbed_uuid": "abs", "survivor_uuid": "sur"}),
        owner_token=owner,
    )


def _state(journal: GraphOperationsJournal, op_id: str) -> str:
    with sqlite3.connect(journal.db_path) as conn:
        return conn.execute("SELECT state FROM graph_operations WHERE op_id = ?", (op_id,)).fetchone()[0]


@pytest.fixture
def events(monkeypatch):
    captured: list[dict] = []
    monkeypatch.setattr(recorders, "record_failure_event", lambda **kw: captured.append(kw) or True)
    return captured


@pytest.mark.unit
def test_commit_lost_to_reconciler_failed_is_recorded(tmp_path, events, caplog):
    journal = GraphOperationsJournal(db_path=tmp_path / "ops.db")
    op_id = _prepare(journal, "owner-A")
    with sqlite3.connect(journal.db_path) as conn:  # reconciler B's claim
        conn.execute("UPDATE graph_operations SET owner_token = 'owner-B' WHERE op_id = ?", (op_id,))
    journal.mark_failed(op_id, reason="abstained: before-state", owner_token="owner-B")

    with caplog.at_level("ERROR"), pytest.raises(StaleTransitionError):
        journal.mark_committed(op_id, owner_token="owner-A")  # A's statement already landed

    assert _state(journal, op_id) == "FAILED", "no new transition: the journal is not rewritten"
    assert len(events) == 1
    event = events[0]
    assert event["failure_stage"] == "commit_refused_after_graph_work"
    assert event["classification"] == "manual_review" and event["retryable"] is False
    assert event["details"]["op_id"] == op_id
    assert event["details"]["operation_kind"] == "ENTITY_MERGE"
    assert event["details"]["journal_state"] == "FAILED"
    assert any("CF-271 F2" in r.getMessage() for r in caplog.records)


@pytest.mark.unit
def test_commit_lost_to_a_live_claim_is_recorded(tmp_path, events):
    """Same record while B still holds the row PREPARED (the F1 shape)."""
    journal = GraphOperationsJournal(db_path=tmp_path / "ops.db")
    op_id = _prepare(journal, "owner-A")
    with sqlite3.connect(journal.db_path) as conn:
        conn.execute("UPDATE graph_operations SET owner_token = 'owner-B' WHERE op_id = ?", (op_id,))

    with pytest.raises(StaleTransitionError):
        journal.mark_committed(op_id, owner_token="owner-A")

    assert _state(journal, op_id) == "PREPARED"
    assert [e["details"]["journal_state"] for e in events] == ["PREPARED"]


@pytest.mark.unit
def test_successful_and_idempotent_commits_record_nothing(tmp_path, events):
    journal = GraphOperationsJournal(db_path=tmp_path / "ops.db")
    op_id = _prepare(journal, "owner-A")
    journal.mark_committed(op_id, owner_token="owner-A")
    journal.mark_committed(op_id, owner_token="owner-A")  # already COMMITTED: idempotent

    assert _state(journal, op_id) == "COMMITTED"
    assert events == []


@pytest.mark.unit
def test_a_failing_recorder_never_masks_the_stale_error(tmp_path, monkeypatch):
    journal = GraphOperationsJournal(db_path=tmp_path / "ops.db")
    op_id = _prepare(journal, "owner-A")
    journal.mark_failed(op_id, reason="reconciler", owner_token="owner-A")

    def boom(**_kw):
        raise RuntimeError("telemetry store down")

    monkeypatch.setattr(recorders, "record_failure_event", boom)
    with pytest.raises(StaleTransitionError):
        journal.mark_committed(op_id, owner_token="owner-A")
