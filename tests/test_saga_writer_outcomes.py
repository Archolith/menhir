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
