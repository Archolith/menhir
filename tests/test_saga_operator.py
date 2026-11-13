"""Operator surface for stranded saga rows (#427 fix B): real journal, fake handlers and gate."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from typer.testing import CliRunner

from menhir.infrastructure.graph_operations import GraphOperationsJournal
from menhir.services import saga_operator as ops
from menhir.services.saga_reconcile_outcomes import (
    WOULD_MARK_ALREADY_APPLIED,
    WOULD_NEEDS_REVIEW,
    WOULD_REPLAY,
)

_REMOTE = "instance-unset:otherhost:4242:0123456789abcdef0123456789abcdef"


class FakeGate:
    def __init__(self, free: bool = True):
        self.free = free
        self.calls: list[str] = []

    def acquire(self) -> bool:
        self.calls.append("acquire")
        return self.free

    def release(self) -> None:
        self.calls.append("release")


class FakeHandler:
    def __init__(self, outcome: str, diag: dict | None = None):
        self.result = (outcome, diag or {})
        self.calls = 0

    def classify_prepared_row(self, row):
        self.calls += 1
        return self.result


@pytest.fixture
def journal(tmp_path) -> GraphOperationsJournal:
    return GraphOperationsJournal(db_path=tmp_path / "ops.db")


def _merge(journal, owner: str = _REMOTE) -> str:
    return journal.prepare(
        operation_kind="ENTITY_MERGE",
        request_json=json.dumps({"survivor_uuid": "sur", "absorbed_uuid": "abs"}),
        target_key="sur|abs",
        owner_token=owner,
    )


def _expire(journal, op_id: str) -> None:
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    with sqlite3.connect(journal.db_path) as conn:
        conn.execute(
            "UPDATE graph_operations SET owner_lease_expires_at = ? WHERE op_id = ?",
            (past, op_id),
        )


def _abandoned(journal) -> str:
    op_id = _merge(journal)
    _expire(journal, op_id)
    assert journal.attest_owner_death(op_id, attested_by="t", expected_owner_token=_REMOTE)
    return op_id


def _row(journal, op_id: str) -> dict:
    with sqlite3.connect(journal.db_path) as conn:
        conn.row_factory = sqlite3.Row
        return dict(
            conn.execute("SELECT * FROM graph_operations WHERE op_id = ?", (op_id,)).fetchone()
        )


def _locks(journal, op_id: str) -> int:
    with sqlite3.connect(journal.db_path) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM graph_operation_locks WHERE op_id = ?", (op_id,)
        ).fetchone()[0]


def _handlers(outcome: str, diag: dict | None = None) -> dict:
    return {"ENTITY_MERGE": FakeHandler(outcome, diag)}


def test_dry_run_writes_nothing(journal):
    op_id = _abandoned(journal)
    before = _row(journal, op_id)
    gate = FakeGate()
    res = ops.resolve(journal, _handlers(WOULD_REPLAY), gate, op_id, by="me")
    assert res["ok"] and res["applied"] is False and res["planned"] == "FAILED"
    assert _row(journal, op_id) == before
    assert gate.calls == []


def test_refuses_live_owner(journal):
    op_id = _merge(journal)
    res = ops.resolve(journal, _handlers(WOULD_REPLAY), FakeGate(), op_id, by="me", apply=True)
    assert res["refused"] and "LIVE_OWNER" in res["reason"]
    assert _row(journal, op_id)["state"] == "PREPARED"


def test_refuses_held_gate(journal):
    op_id = _abandoned(journal)
    gate = FakeGate(free=False)
    res = ops.resolve(journal, _handlers(WOULD_REPLAY), gate, op_id, by="me", apply=True)
    assert res["refused"] and "gate" in res["reason"]
    assert _row(journal, op_id)["state"] == "PREPARED"


def test_refuses_erasure_before_claiming(journal):
    op_id = journal.prepare(
        operation_kind="EXPLICIT_ERASURE",
        request_json=json.dumps({"targets": ["a"]}),
        target_key="erase|a",
        owner_token=_REMOTE,
    )
    _expire(journal, op_id)
    journal.attest_owner_death(op_id, attested_by="t", expected_owner_token=_REMOTE)
    gate = FakeGate()
    res = ops.resolve(
        journal, {"EXPLICIT_ERASURE": FakeHandler(WOULD_REPLAY)}, gate, op_id, by="me", apply=True
    )
    assert res["refused"] and "erasure" in res["reason"]
    assert gate.calls == []
    assert _row(journal, op_id)["owner_token"] == _REMOTE


def test_refuses_ownerless_row(journal):
    op_id = _merge(journal)
    with sqlite3.connect(journal.db_path) as conn:
        conn.execute("UPDATE graph_operations SET owner_token = NULL WHERE op_id = ?", (op_id,))
    res = ops.resolve(journal, _handlers(WOULD_REPLAY), FakeGate(), op_id, by="me", apply=True)
    assert res["refused"] and "needs direct repair" in res["reason"]


def test_missing_by_is_refused(journal):
    op_id = _abandoned(journal)
    assert ops.resolve(journal, _handlers(WOULD_REPLAY), FakeGate(), op_id, by="")["refused"]
    assert ops.attest(journal, op_id, owner_token=_REMOTE, by="")["refused"]
    assert ops.clear_review(journal, op_id, to_state="FAILED", by=" ")["refused"]


def test_replay_outcome_closes_failed_with_claim_token(journal):
    op_id = _abandoned(journal)
    gate = FakeGate()
    res = ops.resolve(journal, _handlers(WOULD_REPLAY), gate, op_id, by="me", apply=True)
    assert res["applied"] and res["planned"] == "FAILED"
    row = _row(journal, op_id)
    assert row["state"] == "FAILED"
    assert row["resolved_by_token"] == res["claim_token"] != _REMOTE
    assert _locks(journal, op_id) == 0
    assert gate.calls == ["acquire", "release"]


def test_already_applied_closes_committed_without_graph_work(journal):
    op_id = _abandoned(journal)
    res = ops.resolve(
        journal, _handlers(WOULD_MARK_ALREADY_APPLIED), FakeGate(), op_id, by="me", apply=True
    )
    assert res["planned"] == "COMMITTED"
    row = _row(journal, op_id)
    assert row["state"] == "COMMITTED" and row["commit_graph_work"] == 0


def test_drift_goes_to_needs_review_and_gate_released(journal):
    op_id = _abandoned(journal)
    gate = FakeGate()
    res = ops.resolve(
        journal,
        _handlers(WOULD_NEEDS_REVIEW, {"observed_error": "drifted"}),
        gate,
        op_id,
        by="me",
        apply=True,
    )
    assert res["planned"] == "NEEDS_REVIEW"
    row = _row(journal, op_id)
    assert row["state"] == "NEEDS_REVIEW" and "drifted" in row["last_error"]
    assert gate.calls[-1] == "release"


def test_delete_all_targets_present_is_before_state_partial_is_not():
    row = {"request_json": json.dumps({"targets": ["a", "b"]})}
    assert ops._is_before_state(
        "ENTITY_DELETE", row, WOULD_NEEDS_REVIEW, {"survivors": ["a", "b"]}
    )
    assert not ops._is_before_state(
        "ENTITY_DELETE", row, WOULD_NEEDS_REVIEW, {"survivors": ["a"]}
    )


def test_attestation_bound_to_expected_token(journal):
    op_id = _merge(journal)
    _expire(journal, op_id)
    bad = ops.attest(journal, op_id, owner_token="someone-else", by="me")
    assert bad["refused"] and bad["recorded"] is False
    assert ops.attest(journal, op_id, owner_token=_REMOTE, by="me")["recorded"] is True


def test_clear_review_stamps_operator(journal):
    op_id = _abandoned(journal)
    ops.resolve(
        journal, _handlers(WOULD_NEEDS_REVIEW, {"observed_error": "x"}), FakeGate(), op_id,
        by="me", apply=True,
    )
    assert ops.clear_review(journal, op_id, to_state="FAILED", by="alice")["ok"]
    assert _row(journal, op_id)["resolved_by_token"] == "operator:alice"


def test_list_and_show_are_read_only(journal):
    op_id = _abandoned(journal)
    listed = ops.list_operations(journal)["operations"]
    assert [o["op_id"] for o in listed] == [op_id] and listed[0]["ownership"] == "ABANDONED"
    before = _row(journal, op_id)
    shown = ops.show_operation(journal, _handlers(WOULD_REPLAY), op_id)
    assert shown["verdict"]["outcome"] == WOULD_REPLAY
    assert _row(journal, op_id) == before


def test_cli_help_lists_five_commands():
    from menhir.cli import app

    out = CliRunner().invoke(app, ["saga", "--help"]).output
    for name in ("list", "show", "attest", "resolve", "clear-review"):
        assert name in out
