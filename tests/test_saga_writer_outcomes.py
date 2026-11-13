"""Writer-side settlement for #427 (rollback -> FAILED) and #404 (refused commit after graph work).

Each test names the bad outcome it makes impossible:

* #427: a row stays PREPARED (locked, boot-blocking) after a write the server provably rolled back;
  or, the converse, a row goes FAILED when the commit may have landed (connection-class error).
* #404: a refused commit after this writer's graph work is reported as success when two owners
  may each have applied it; or a single verified application is reported as a failure.
* #404 FR-4: a replay refused mid-transition reports LIVE_OWNER (a live writer that does not exist)
  or lets a live boot proceed.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from neo4j.exceptions import (
    ClientError,
    ConstraintError,
    DatabaseError,
    ServiceUnavailable,
    SessionExpired,
    TransientError,
)

from menhir.infrastructure import operation_owner as oo
from menhir.infrastructure.graph_operations import GraphOperationsJournal
from menhir.infrastructure.graph_operations_transitions import StaleTransitionError
from menhir.services.saga_reconcile_dispatcher import SagaReconcileDispatcher
from menhir.services.saga_reconcile_outcomes import STALE
from menhir.services.saga_writer_outcomes import (
    COMMITTED_BY_THIS_WRITER,
    COMPLETED_BY_OTHER_OWNER,
    close_after_failed_mutation,
    commit_or_settle,
    server_rolled_back,
    settle_refused_commit,
)

pytestmark = pytest.mark.unit

_REMOTE = "instance-unset:otherhost:4242:0123456789abcdef0123456789abcdef"


@pytest.fixture
def journal(tmp_path) -> GraphOperationsJournal:
    return GraphOperationsJournal(db_path=tmp_path / "ops.db")


def _merge(journal, owner: str) -> str:
    return journal.prepare(
        operation_kind="ENTITY_MERGE",
        request_json=json.dumps({"survivor_uuid": "sur", "absorbed_uuid": "abs"}),
        target_key="sur|abs",
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


def _locks(journal, op_id: str) -> set[str]:
    with sqlite3.connect(journal.db_path) as conn:
        return {r[0] for r in conn.execute(
            "SELECT entity_uuid FROM graph_operation_locks WHERE op_id = ?", (op_id,)
        )}


def _taken_over(journal) -> tuple[str, str]:
    """Writer W prepared; a remote owner R judged it dead and claimed the row."""
    writer = oo.new_claim_token()
    op_id = _merge(journal, writer)
    _expire_and_attest(journal, op_id)
    assert journal.claim_abandoned_operation(op_id, owner_token=_REMOTE)
    return op_id, writer


# ------------------------------------------------------------------ server_rolled_back
@pytest.mark.parametrize("exc", [
    ClientError("syntax"),
    ConstraintError("node still has relationships"),
    TransientError("deadlock"),
])
def test_server_reported_statement_errors_prove_rollback(exc):
    assert server_rolled_back(exc) is True


@pytest.mark.parametrize("exc", [
    ServiceUnavailable("socket closed"),
    SessionExpired("session gone"),
    DatabaseError("internal"),
    RuntimeError("raised after the statement may have committed"),
])
def test_ambiguous_errors_prove_nothing(exc):
    assert server_rolled_back(exc) is False


# ------------------------------------------------------------------ #427 close_after_failed_mutation
def test_server_rollback_with_before_state_proof_closes_failed_and_frees_the_pair(journal):
    token = oo.new_claim_token()
    op_id = _merge(journal, token)
    closed = close_after_failed_mutation(
        journal, op_id, owner_token=token, exc=ConstraintError("rel"), prove_before=lambda: True,
    )
    assert closed is True
    row = journal.get(op_id)
    assert row["state"] == "FAILED"
    assert "rolled back by the server" in row["last_error"]
    assert row["attempt_count"] == 1
    assert _locks(journal, op_id) == set()
    # The pair is mergeable again: a fresh op on the same participants and target key prepares.
    assert _merge(journal, oo.new_claim_token())


@pytest.mark.parametrize("exc", [ServiceUnavailable("lost"), SessionExpired("gone")])
def test_connection_errors_leave_the_row_prepared_even_if_the_graph_looks_unchanged(journal, exc):
    token = oo.new_claim_token()
    op_id = _merge(journal, token)
    proofs = []
    closed = close_after_failed_mutation(
        journal, op_id, owner_token=token, exc=exc,
        prove_before=lambda: proofs.append(1) or True,
    )
    assert closed is False
    assert proofs == [], "an ambiguous error must not even be argued from a re-read"
    row = journal.get(op_id)
    assert row["state"] == "PREPARED" and row["attempt_count"] == 1
    assert _locks(journal, op_id) == {"sur", "abs"}


def test_rollback_without_before_state_proof_stays_prepared(journal):
    token = oo.new_claim_token()
    op_id = _merge(journal, token)
    assert close_after_failed_mutation(
        journal, op_id, owner_token=token, exc=ClientError("x"), prove_before=lambda: False,
    ) is False
    assert journal.get(op_id)["state"] == "PREPARED"


def test_unreadable_graph_proves_nothing(journal):
    token = oo.new_claim_token()
    op_id = _merge(journal, token)

    def boom():
        raise ServiceUnavailable("re-read failed")

    assert close_after_failed_mutation(
        journal, op_id, owner_token=token, exc=ClientError("x"), prove_before=boom,
    ) is False
    assert journal.get(op_id)["state"] == "PREPARED"


def test_a_writer_that_lost_its_claim_does_not_close_the_row(journal):
    op_id, writer = _taken_over(journal)
    proofs = []
    assert close_after_failed_mutation(
        journal, op_id, owner_token=writer, exc=ClientError("x"),
        prove_before=lambda: proofs.append(1) or True,
    ) is False
    assert proofs == []
    row = journal.get(op_id)
    assert row["state"] == "PREPARED" and row["owner_token"] == _REMOTE
    assert row["attempt_count"] == 0


# ------------------------------------------------------------------ #404 settle_refused_commit
def test_writer_did_work_and_row_was_failed_escalates(journal):
    op_id, writer = _taken_over(journal)
    journal.mark_failed(op_id, owner_token=_REMOTE, reason="reconciler: before-state")
    assert settle_refused_commit(
        journal, op_id, owner_token=writer, did_graph_work=True, observed="landed",
    ) is False
    assert journal.get(op_id)["state"] == "NEEDS_REVIEW"
    assert _locks(journal, op_id) == {"sur", "abs"}


def test_writer_did_no_work_and_row_was_failed_leaves_failed(journal):
    op_id, writer = _taken_over(journal)
    journal.mark_failed(op_id, owner_token=_REMOTE, reason="reconciler: before-state")
    assert settle_refused_commit(
        journal, op_id, owner_token=writer, did_graph_work=False, observed="skip",
    ) is False
    assert journal.get(op_id)["state"] == "FAILED"
    assert _locks(journal, op_id) == set()


@pytest.mark.parametrize("committer_work", [True, None])
def test_both_owners_did_work_or_committer_unknown_escalates(journal, committer_work):
    op_id, writer = _taken_over(journal)
    journal.mark_committed(op_id, owner_token=_REMOTE, after_graph_work=bool(committer_work))
    if committer_work is None:  # an older binary, which never wrote the column
        with sqlite3.connect(journal.db_path) as conn:
            conn.execute(
                "UPDATE graph_operations SET commit_graph_work = NULL WHERE op_id = ?", (op_id,)
            )
    assert settle_refused_commit(
        journal, op_id, owner_token=writer, did_graph_work=True, observed="double?",
    ) is False
    assert journal.get(op_id)["state"] == "NEEDS_REVIEW"


def test_writer_did_work_and_committer_only_observed_it_is_one_application(journal):
    op_id, writer = _taken_over(journal)
    journal.mark_committed(op_id, owner_token=_REMOTE, after_graph_work=False)
    assert settle_refused_commit(
        journal, op_id, owner_token=writer, did_graph_work=True, observed="x",
    ) is True
    assert journal.get(op_id)["state"] == "COMMITTED"


@pytest.mark.parametrize("committer_work", [True, False])
def test_writer_did_no_work_and_another_owner_committed_is_success(journal, committer_work):
    op_id, writer = _taken_over(journal)
    journal.mark_committed(op_id, owner_token=_REMOTE, after_graph_work=committer_work)
    assert settle_refused_commit(
        journal, op_id, owner_token=writer, did_graph_work=False, observed="x",
    ) is True
    assert journal.get(op_id)["state"] == "COMMITTED"


def test_any_other_state_is_reraised_untouched(journal):
    op_id, writer = _taken_over(journal)
    assert settle_refused_commit(
        journal, op_id, owner_token=writer, did_graph_work=True, observed="x",
    ) is False
    row = journal.get(op_id)
    assert row["state"] == "PREPARED" and row["owner_token"] == _REMOTE


# ------------------------------------------------------------------ commit_or_settle
def test_commit_or_settle_commits_for_the_owner(journal):
    token = oo.new_claim_token()
    op_id = _merge(journal, token)
    assert commit_or_settle(
        journal, op_id, owner_token=token, after_graph_work=True, observed="x",
    ) == COMMITTED_BY_THIS_WRITER
    row = journal.get(op_id)
    assert row["state"] == "COMMITTED" and row["commit_graph_work"] == 1


def test_commit_or_settle_reports_single_application_by_other_owner(journal):
    op_id, writer = _taken_over(journal)
    journal.mark_committed(op_id, owner_token=_REMOTE, after_graph_work=False)
    assert commit_or_settle(
        journal, op_id, owner_token=writer, after_graph_work=True, observed="x",
    ) == COMPLETED_BY_OTHER_OWNER


def test_commit_or_settle_reraises_a_possible_double_apply(journal):
    op_id, writer = _taken_over(journal)
    journal.mark_committed(op_id, owner_token=_REMOTE, after_graph_work=True)
    with pytest.raises(StaleTransitionError):
        commit_or_settle(
            journal, op_id, owner_token=writer, after_graph_work=True, observed="x",
        )
    assert journal.get(op_id)["state"] == "NEEDS_REVIEW"


# ------------------------------------------------------------------ dispatcher STALE (#404 FR-4)
class _Gate:
    lease_duration_s = 3600.0

    def verify_still_held(self):
        return None

    def renew(self):
        return True


class _StaleHandler:
    def __init__(self):
        self.tokens: list[str] = []

    def classify_prepared_row(self, row):
        return "WOULD_REPLAY", {}

    def replay_prepared_row(self, row, *, owner_token):
        self.tokens.append(owner_token)
        raise StaleTransitionError("moved under us")


@pytest.mark.usefixtures("pid_namespace_verifiable")
def test_a_stale_replay_blocks_a_live_boot_and_is_not_reported_as_a_live_owner(journal):
    op_id = journal.prepare(
        operation_kind="ENTITY_MERGE",
        request_json=json.dumps({"survivor_uuid": "sur", "absorbed_uuid": "abs"}),
        owner_token=_REMOTE,
    )
    _expire_and_attest(journal, op_id)
    handler = _StaleHandler()
    run = SagaReconcileDispatcher(
        journal=journal, handlers={"ENTITY_MERGE": handler},
    ).run(dry_run=False, gate=_Gate())

    assert run.counts.get(STALE) == 1
    assert run.counts.get("LIVE_OWNER", 0) == 0
    assert run.write_ready is False
    assert run.blocking_reasons
    # The handler got the per-claim token the dispatcher claimed the row with.
    assert len(handler.tokens) == 1
    assert journal.get(op_id)["owner_token"] == handler.tokens[0]
    assert oo.same_process(handler.tokens[0], process_token=oo.process_owner_token())


# ------------------------------------------------------------------ review F4 / F14
from menhir.services.saga_writer_outcomes import quarantine_after_graph_work  # noqa: E402


@pytest.mark.parametrize("closer", ["failed", "committed"])
def test_writer_after_state_drift_on_a_row_another_owner_closed_escalates(journal, closer):
    """Review F4: the writer mutated, its after-state check failed, and a reconciler had already
    closed the row; the refused NEEDS_REVIEW must re-fence and quarantine, not leave FAILED (no
    mutation) or COMMITTED (verified) standing over a graph this writer just changed."""
    op_id, writer = _taken_over(journal)
    if closer == "failed":
        journal.mark_failed(op_id, owner_token=_REMOTE, reason="reconciler: before-state")
    else:
        journal.mark_committed(op_id, owner_token=_REMOTE, after_graph_work=False)
    with pytest.raises(StaleTransitionError):
        quarantine_after_graph_work(
            journal, op_id, owner_token=writer, observed_error="after-state mismatch",
        )
    assert journal.get(op_id)["state"] == "NEEDS_REVIEW"
    assert _locks(journal, op_id) == {"sur", "abs"}


def test_writer_quarantine_on_its_own_row_is_a_plain_needs_review(journal):
    token = oo.new_claim_token()
    op_id = _merge(journal, token)
    quarantine_after_graph_work(journal, op_id, owner_token=token, observed_error="drift")
    row = journal.get(op_id)
    assert row["state"] == "NEEDS_REVIEW" and row["last_error"] == "drift"


def test_bookkeeping_failure_never_replaces_the_mutation_error(journal):
    """Review F14: if the journal itself fails, the caller still re-raises its own Neo4j error."""

    class _Broken:
        def record_attempt(self, *a, **k):
            raise RuntimeError("sqlite locked")

    assert close_after_failed_mutation(
        _Broken(), "op", owner_token="t", exc=ClientError("x"), prove_before=lambda: True,
    ) is False


# ------------------------------------------------------------------ review F5 / F16 (#404 F-4)
def test_a_replay_whose_row_was_completed_under_its_claim_is_stale_not_replayed(journal):
    """Review F16: if another owner closes the row while this replay holds its claim, the replay
    lost ownership mid-flight; reporting REPLAYED would let a live boot treat that as clean."""
    from menhir.services.delete_coordinator import DeleteCoordinator

    class _Gone:
        def capture_node_state(self, uuid):
            return None

    other = oo.new_claim_token()
    op_id = journal.prepare(
        operation_kind="ENTITY_DELETE",
        request_json=json.dumps({"targets": ["n1"]}),
        target_uuid="n1",
        owner_token=other,
    )
    stale_snapshot = journal.get(op_id)
    journal.mark_committed(op_id, owner_token=other, after_graph_work=False)
    outcome, diag = DeleteCoordinator(graph_adapter=_Gone(), journal=journal).replay_prepared_row(
        stale_snapshot, owner_token=oo.new_claim_token(),
    )
    assert outcome == STALE and diag["stale"] is True
    assert journal.get(op_id)["state"] == "COMMITTED"


@pytest.mark.usefixtures("pid_namespace_verifiable")
def test_this_process_s_own_attested_row_blocks_a_live_boot(journal):
    """Review F5: the claim refuses this process's own rows (F-3), so an attested row left by an
    earlier startup attempt in this PID must still block readiness, not read as a healthy peer."""
    own = oo.new_claim_token()
    op_id = _merge(journal, own)
    _expire_and_attest(journal, op_id)
    run = SagaReconcileDispatcher(
        journal=journal, handlers={"ENTITY_MERGE": _StaleHandler()},
    ).run(dry_run=False, gate=_Gate())
    assert run.write_ready is False
    assert journal.get(op_id)["state"] == "PREPARED"
