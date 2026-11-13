"""Journal-level counterexamples for #404 and #406 (plan menhir-427-saga-journal-truth).

Each test names the bad outcome it makes impossible:

* F-3: after P -> R -> P, P's ORIGINAL stale writer must not match the row again.
* F2 escalation: a writer whose graph work landed after its row went FAILED/COMMITTED must be able
  to re-fence and quarantine it -- and must be refused when that would collide with live work.
* F-6/FR-2/FR-3/FR-4/F-7: no tokenless mark, no ownerless re-open, an operator clear names the
  operator.
* F-8/F-9: no commit-refused record without graph work; a stale writer's evidence is not dropped.
"""

from __future__ import annotations

import functools
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from menhir.infrastructure import operation_owner as oo
from menhir.infrastructure.graph_operations import GraphOperationsJournal
from menhir.infrastructure.graph_operations_model import GraphOperationError
from menhir.infrastructure.graph_operations_transitions import (
    ESCALATED,
    REFUSAL_COMMITTED_BY_OTHER,
    REFUSAL_ILLEGAL_TRANSITION,
    REFUSED_FENCE_TAKEN,
    REFUSED_NOT_CLAIMANT,
    REFUSED_STATE,
    StaleTransitionError,
)
from menhir.infrastructure.telemetry import recorders
from menhir.infrastructure.telemetry.store import McpTelemetryStore

pytestmark = pytest.mark.unit

#: A token another process would have minted: different host, so this process never claims it as
#: its own, and its PID can only be proven dead by attestation.
_REMOTE = "instance-unset:otherhost:4242:0123456789abcdef0123456789abcdef"


@pytest.fixture
def journal(tmp_path) -> GraphOperationsJournal:
    return GraphOperationsJournal(db_path=tmp_path / "ops.db")


@pytest.fixture
def store(tmp_path, monkeypatch) -> McpTelemetryStore:
    real = McpTelemetryStore(db_path=tmp_path / "telemetry.db")
    real._ensure_ready()
    monkeypatch.setattr(
        recorders, "record_failure_event",
        functools.partial(recorders.record_failure_event, store=real),
    )
    return real


def _events(store: McpTelemetryStore, stage: str) -> list[dict]:
    with sqlite3.connect(store.db_path) as conn:
        rows = conn.execute(
            "SELECT details_json FROM failure_events WHERE failure_stage = ?", (stage,)
        ).fetchall()
    return [json.loads(r[0]) for r in rows]


def _merge(journal, owner: str, *, survivor: str = "sur", absorbed: str = "abs") -> str:
    return journal.prepare(
        operation_kind="ENTITY_MERGE",
        request_json=json.dumps({"survivor_uuid": survivor, "absorbed_uuid": absorbed}),
        target_key=f"{survivor}|{absorbed}",
        owner_token=owner,
    )


def _expire_and_attest(journal, op_id: str) -> None:
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    with sqlite3.connect(journal.db_path) as conn:
        conn.execute(
            "UPDATE graph_operations SET owner_lease_expires_at = ? WHERE op_id = ?",
            (past, op_id),
        )
    assert journal.attest_owner_death(op_id, attested_by="test operator")


def _row(journal, op_id: str) -> dict:
    with sqlite3.connect(journal.db_path) as conn:
        conn.row_factory = sqlite3.Row
        return dict(conn.execute(
            "SELECT * FROM graph_operations WHERE op_id = ?", (op_id,)
        ).fetchone())


def _locks(journal, op_id: str) -> set[str]:
    with sqlite3.connect(journal.db_path) as conn:
        return {r[0] for r in conn.execute(
            "SELECT entity_uuid FROM graph_operation_locks WHERE op_id = ?", (op_id,)
        )}


def _claims(journal, op_id: str) -> list[tuple[str, str]]:
    with sqlite3.connect(journal.db_path) as conn:
        return conn.execute(
            "SELECT claim_token, claim_kind FROM graph_operation_claims WHERE op_id = ? "
            "ORDER BY rowid", (op_id,)
        ).fetchall()


# ------------------------------------------------------------------ claim tokens (#404 F-3)
def test_claim_token_keeps_host_and_pid_readable_and_is_unique_per_claim():
    a, b = oo.new_claim_token(), oo.new_claim_token()
    assert a != b
    assert oo._owner_parts(a) == oo._owner_parts(oo.process_owner_token())
    assert oo.same_process(a) and oo.same_process(b)
    assert oo.process_part(a) == oo.process_owner_token()
    assert not oo.same_process(_REMOTE)
    assert oo.is_own_claim({"owner_token": a})


def test_f3_takeover_chain_does_not_revalidate_the_original_stale_writer(journal):
    p_writer = oo.new_claim_token()
    op_id = _merge(journal, p_writer)
    # Wrongly attested dead; a remote reconciler R claims, then R crashes and is attested.
    _expire_and_attest(journal, op_id)
    assert journal.claim_abandoned_operation(op_id, owner_token=_REMOTE)
    _expire_and_attest(journal, op_id)
    # P's own reconciler claims it back -- under a NEW claim token.
    p_reconciler = oo.new_claim_token()
    assert journal.claim_abandoned_operation(op_id, owner_token=p_reconciler)

    with pytest.raises(StaleTransitionError):
        journal.mark_failed(op_id, owner_token=p_writer, reason="stale P writer abstained")
    row = _row(journal, op_id)
    assert row["state"] == "PREPARED" and row["owner_token"] == p_reconciler
    assert _locks(journal, op_id) == {"sur", "abs"}
    assert [k for _, k in _claims(journal, op_id)] == ["prepare", "takeover", "takeover"]


def test_a_process_still_does_not_claim_its_own_row_under_a_new_token(journal):
    mine = oo.new_claim_token()
    op_id = _merge(journal, mine)
    _expire_and_attest(journal, op_id)
    assert not journal.claim_abandoned_operation(op_id, owner_token=oo.new_claim_token())


def test_attestation_binds_to_the_token_the_operator_inspected(journal):
    op_id = _merge(journal, _REMOTE)
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    with sqlite3.connect(journal.db_path) as conn:
        conn.execute(
            "UPDATE graph_operations SET owner_lease_expires_at = ? WHERE op_id = ?",
            (past, op_id),
        )
    assert not journal.attest_owner_death(
        op_id, attested_by="op", expected_owner_token="someone-else"
    )
    assert journal.attest_owner_death(op_id, attested_by="op", expected_owner_token=_REMOTE)


# ------------------------------------------------------------------ fail-closed API (#406 F-6)
@pytest.mark.parametrize("method", ["mark_committed", "mark_failed", "mark_needs_review",
                                    "record_attempt", "mark_reversed"])
def test_every_mark_refuses_a_missing_none_or_empty_token(journal, method):
    op_id = _merge(journal, "owner-A")
    with pytest.raises(TypeError):
        getattr(journal, method)(op_id)
    for bad in (None, "", "   "):
        with pytest.raises(GraphOperationError):
            getattr(journal, method)(op_id, owner_token=bad)
    assert _row(journal, op_id)["state"] == "PREPARED"


def test_record_attempt_always_checks_the_claim(journal):
    op_id = _merge(journal, "owner-A")
    assert journal.record_attempt(op_id, owner_token="owner-B", error="x") is False
    assert journal.record_attempt(op_id, owner_token="owner-A", error="y") is True
    assert _row(journal, op_id)["attempt_count"] == 1


# ------------------------------------------------------------------ operator clear (F-7, FR-2..4)
def test_clear_needs_review_refuses_prepared_and_requires_an_operator(journal):
    op_id = _merge(journal, "owner-A")
    journal.mark_needs_review(op_id, owner_token="owner-A", observed_error="drift")
    with pytest.raises(GraphOperationError):
        journal.clear_needs_review(op_id, to_state="PREPARED", operator="alice")
    with pytest.raises(GraphOperationError):
        journal.clear_needs_review(op_id, to_state="FAILED", operator="  ")
    assert _row(journal, op_id)["state"] == "NEEDS_REVIEW"
    assert _locks(journal, op_id) == {"sur", "abs"}


def test_operator_clear_names_the_operator_and_a_stale_writer_commit_is_refused(journal):
    op_id = _merge(journal, "owner-A")
    journal.mark_needs_review(op_id, owner_token="owner-A", observed_error="drift")
    journal.clear_needs_review(op_id, to_state="COMMITTED", operator="alice")
    assert _row(journal, op_id)["resolved_by_token"] == "operator:alice"
    assert _locks(journal, op_id) == set()
    # FR-2/FR-3: the original writer is not told the operator's commit was its own.
    with pytest.raises(StaleTransitionError) as exc:
        journal.mark_committed(op_id, owner_token="owner-A")
    assert exc.value.refusal == REFUSAL_COMMITTED_BY_OTHER


# ------------------------------------------------------------------ F-8 / F-9 / FR-7
def test_commit_records_whether_the_committer_did_graph_work(journal):
    a = _merge(journal, "owner-A")
    b = _merge(journal, "owner-B", survivor="s2", absorbed="a2")
    journal.mark_committed(a, owner_token="owner-A")
    journal.mark_committed(b, owner_token="owner-B", after_graph_work=False)
    assert _row(journal, a)["commit_graph_work"] == 1
    assert _row(journal, b)["commit_graph_work"] == 0


def test_refused_commit_without_graph_work_leaves_no_manual_review_record(journal, store):
    op_id = _merge(journal, "owner-A")
    journal.mark_failed(op_id, owner_token="owner-A", reason="abstained")
    with pytest.raises(StaleTransitionError):
        journal.mark_committed(op_id, owner_token="owner-A", after_graph_work=False)
    assert _events(store, "commit_refused_after_graph_work") == []
    with pytest.raises(StaleTransitionError) as exc:
        journal.mark_committed(op_id, owner_token="owner-A")
    assert exc.value.refusal == REFUSAL_ILLEGAL_TRANSITION
    [event] = _events(store, "commit_refused_after_graph_work")
    assert event["op_id"] == op_id and event["refusal"] == REFUSAL_ILLEGAL_TRANSITION


def test_f9_stale_writer_evidence_is_recorded_not_dropped(journal, store):
    op_id = _merge(journal, "owner-R")
    journal.mark_needs_review(op_id, owner_token="owner-R", observed_error="R drift")
    journal.mark_needs_review(op_id, owner_token="owner-P", observed_error="P mismatch")
    assert _row(journal, op_id)["last_error"] == "R drift"
    [event] = _events(store, "stale_writer_evidence")
    assert event["op_id"] == op_id and event["journal_state"] == "NEEDS_REVIEW"
    # The same owner repeating its own move is not evidence of anything.
    journal.mark_needs_review(op_id, owner_token="owner-R", observed_error="R again")
    assert len(_events(store, "stale_writer_evidence")) == 1


# ------------------------------------------------------------------ escalation (#404 F2)
def _failed_by_reconciler(journal) -> tuple[str, str]:
    """Writer W prepares; a remote reconciler R claims and marks FAILED (W wrongly judged dead)."""
    writer = oo.new_claim_token()
    op_id = _merge(journal, writer)
    _expire_and_attest(journal, op_id)
    assert journal.claim_abandoned_operation(op_id, owner_token=_REMOTE)
    journal.mark_failed(op_id, owner_token=_REMOTE, reason="reconciler: before-state")
    assert _locks(journal, op_id) == set()
    return op_id, writer


def test_writer_landing_after_failed_escalates_and_refences(journal, store):
    op_id, writer = _failed_by_reconciler(journal)
    with pytest.raises(StaleTransitionError):
        journal.mark_committed(op_id, owner_token=writer)
    assert journal.escalate_after_refused_commit(
        op_id, owner_token=writer, observed_error="writer landed after FAILED"
    ) == ESCALATED
    row = _row(journal, op_id)
    assert row["state"] == "NEEDS_REVIEW"
    assert row["last_error"] == "writer landed after FAILED"
    assert _locks(journal, op_id) == {"sur", "abs"}
    assert _events(store, "commit_refused_escalation")[0]["escalation"] == ESCALATED


def test_escalation_is_refused_when_another_op_took_a_participant(journal, store):
    op_id, writer = _failed_by_reconciler(journal)
    other = _merge(journal, "owner-X", survivor="sur", absorbed="third")
    assert journal.escalate_after_refused_commit(
        op_id, owner_token=writer, observed_error="landed"
    ) == REFUSED_FENCE_TAKEN
    assert _row(journal, op_id)["state"] == "FAILED"
    assert _locks(journal, op_id) == set()  # the partial re-lock rolled back
    assert _locks(journal, other) == {"sur", "third"}
    assert _events(store, "commit_refused_escalation")[0]["escalation"] == REFUSED_FENCE_TAKEN


def test_escalation_is_refused_when_a_newer_op_holds_the_target_key(journal):
    writer = "owner-W"
    op_id = journal.prepare(
        operation_kind="METRIC_WRITE",
        request_json=json.dumps({"view_key": "k"}),
        target_key="metric:k",
        owner_token=writer,
    )
    journal.mark_failed(op_id, owner_token=writer, reason="rolled back")
    journal.prepare(
        operation_kind="METRIC_WRITE",
        request_json=json.dumps({"view_key": "k"}),
        target_key="metric:k",
        owner_token="owner-newer",
    )
    assert journal.escalate_after_refused_commit(
        op_id, owner_token=writer, observed_error="landed"
    ) == REFUSED_FENCE_TAKEN
    assert _row(journal, op_id)["state"] == "FAILED"


def test_escalation_requires_a_token_that_held_a_claim_on_this_op(journal):
    op_id, _writer = _failed_by_reconciler(journal)
    assert journal.escalate_after_refused_commit(
        op_id, owner_token=oo.new_claim_token(), observed_error="x"
    ) == REFUSED_NOT_CLAIMANT
    assert _row(journal, op_id)["state"] == "FAILED"


def test_escalation_refuses_a_row_that_is_not_failed_or_committed(journal):
    writer = "owner-W"
    op_id = _merge(journal, writer)
    assert journal.escalate_after_refused_commit(
        op_id, owner_token=writer, observed_error="x"
    ) == REFUSED_STATE
    assert _row(journal, op_id)["state"] == "PREPARED"


def test_writer_landing_after_another_owner_committed_can_escalate(journal):
    writer = oo.new_claim_token()
    op_id = _merge(journal, writer)
    _expire_and_attest(journal, op_id)
    assert journal.claim_abandoned_operation(op_id, owner_token=_REMOTE)
    journal.mark_committed(op_id, owner_token=_REMOTE)
    with pytest.raises(StaleTransitionError) as exc:
        journal.mark_committed(op_id, owner_token=writer)
    assert exc.value.refusal == REFUSAL_COMMITTED_BY_OTHER
    assert journal.escalate_after_refused_commit(
        op_id, owner_token=writer, observed_error="possible double apply"
    ) == ESCALATED
    assert _row(journal, op_id)["state"] == "NEEDS_REVIEW"
    assert _locks(journal, op_id) == {"sur", "abs"}
