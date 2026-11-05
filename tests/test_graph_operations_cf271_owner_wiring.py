"""CF-271 second pass: owner-token wiring, real attest/claim interleavings, record_attempt guard.

Offline; every journal here is a real SQLite file and every takeover goes through the real
``attest_owner_death`` + ``claim_abandoned_operation`` (no raw-UPDATE takeover, no MagicMock journal).
"""

from __future__ import annotations

import ast
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import menhir
import menhir.services.merge_coordinator as mc
from menhir.infrastructure import operation_owner as oo
from menhir.infrastructure.graph_operations import GraphOperationsJournal
from menhir.infrastructure.graph_operations_model import GraphOperationError
from menhir.infrastructure.graph_operations_transitions import StaleTransitionError
from menhir.services.merge_coordinator import MergeCoordinator
from menhir.services.saga_reconcile_dispatcher import (
    NonReplayableKind,
    SagaReconcileDispatcher,
)
from menhir.services.saga_reconcile_outcomes import LIVE_OWNER

_PAST = "2000-01-01T00:00:00+00:00"
_R_TOKEN = "instance-R:other-host:4242:rrrr"


@pytest.fixture
def journal(tmp_path):
    return GraphOperationsJournal(db_path=tmp_path / "ops.db")


def _prepare(journal, *, a="abs", s="sur"):
    # default owner token == oo.process_owner_token(), exactly what a coordinator's PREPARE stamps
    return journal.prepare(
        operation_kind="ENTITY_MERGE",
        request_json=json.dumps({"absorbed_uuid": a, "survivor_uuid": s}),
    )


def _row(journal, op_id):
    with sqlite3.connect(journal.db_path) as conn:
        return conn.execute(
            "SELECT state, owner_token, last_error, attempt_count FROM graph_operations "
            "WHERE op_id = ?",
            (op_id,),
        ).fetchone()


def _locks(journal, op_id):
    with sqlite3.connect(journal.db_path) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM graph_operation_locks WHERE op_id = ?", (op_id,)
        ).fetchone()[0]


def _expire(journal, op_id):
    with sqlite3.connect(journal.db_path) as conn:
        conn.execute(
            "UPDATE graph_operations SET owner_lease_expires_at = ? WHERE op_id = ?",
            (_PAST, op_id),
        )
        conn.commit()


def _real_takeover(journal, op_id, *, new_owner=_R_TOKEN):
    """W's lease lapses, an operator attests its death, reconciler R claims. All real journal calls."""
    _expire(journal, op_id)
    assert journal.attest_owner_death(op_id, attested_by="operator") is True
    assert journal.claim_abandoned_operation(op_id, owner_token=new_owner) is True


# ------------------------------------------------------------------------------ AST guard
_MARKS = {"mark_committed", "mark_failed", "mark_needs_review"}


@pytest.mark.unit
def test_every_production_terminal_mark_passes_owner_token():
    src_root = Path(menhir.__file__).parent
    offenders: list[str] = []
    seen = 0
    for path in sorted(src_root.rglob("*.py")):
        if path.name == "graph_operations_transitions.py":
            continue  # the definitions themselves
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in _MARKS
                # journal calls only; migration_batches has its own unrelated mark_* methods
                and "journal" in ast.unparse(node.func.value)
            ):
                seen += 1
                if not any(kw.arg == "owner_token" for kw in node.keywords):
                    offenders.append(f"{path.relative_to(src_root)}:{node.lineno} {node.func.attr}")
    assert seen > 20, "the walker found suspiciously few call sites; the guard is not looking"
    assert not offenders, "journal mark call(s) without owner_token=: " + ", ".join(offenders)


# ------------------------------------------------------------------ S1: W stalls, R claims
def _w_coordinator(journal, op_id, *, on_stall):
    coord = MergeCoordinator(graph_adapter=MagicMock(), journal=journal)
    coord.graph_adapter.merge_entity.return_value = {"merged": 0, "reason": "GATE"}
    coord.graph_adapter.fetch_merge_state.side_effect = lambda *_a, **_k: on_stall() or {}
    coord._classify_replay = lambda request: (
        "WOULD_REPLAY",
        {"expected_before": "fp-before", "expected_after": "fp-after"},
    )
    return coord


def _request(op_id):
    return {
        "op_id": op_id, "survivor_uuid": "sur", "absorbed_uuid": "abs",
        "similarity": 0.9, "expected_fact_uuids": [],
    }


@pytest.mark.unit
def test_s1_stale_writer_mark_failed_cannot_release_a_row_the_reconciler_owns(
    journal, monkeypatch
):
    op_id = _prepare(journal)
    assert _locks(journal, op_id) == 2
    monkeypatch.setattr(mc, "merge_state_fingerprint", lambda *_a, **_k: "fp-before")

    # W abstains and reads the pristine before-state; while it is stalled before mark_failed,
    # the real attest+claim hands the row to R.
    coord = _w_coordinator(journal, op_id, on_stall=lambda: _real_takeover(journal, op_id))
    with pytest.raises(StaleTransitionError):
        coord._apply_owned(_request(op_id))

    state, owner, last_error, _ = _row(journal, op_id)
    assert state == "PREPARED"
    assert owner == _R_TOKEN
    assert last_error is None
    assert _locks(journal, op_id) == 2  # the fence is intact

    # R finishes: its own commit, carrying its own token, succeeds and releases the fence.
    journal.mark_committed(op_id, owner_token=_R_TOKEN)
    state, owner, _, _ = _row(journal, op_id)
    assert (state, owner) == ("COMMITTED", None)
    assert _locks(journal, op_id) == 0


@pytest.mark.unit
def test_s1b_stale_writer_mark_committed_and_needs_review_are_refused(journal, monkeypatch):
    op_id = _prepare(journal)
    _real_takeover(journal, op_id)
    me = oo.process_owner_token()
    with pytest.raises(StaleTransitionError):
        journal.mark_committed(op_id, owner_token=me)
    with pytest.raises(StaleTransitionError):
        journal.mark_needs_review(op_id, observed_error="stale", owner_token=me)
    with pytest.raises(StaleTransitionError):
        journal.mark_failed(op_id, reason="stale", owner_token=me)
    assert _row(journal, op_id)[:3] == ("PREPARED", _R_TOKEN, None)
    assert _locks(journal, op_id) == 2


# ------------------------------------------------------------------ F3 / F9
@pytest.mark.unit
def test_stale_needs_review_cannot_downgrade_a_committed_row(journal):
    op_id = _prepare(journal)
    journal.mark_committed(op_id, owner_token=oo.process_owner_token())
    with pytest.raises(StaleTransitionError):
        journal.mark_needs_review(
            op_id, observed_error="stale drift", owner_token=oo.process_owner_token()
        )
    assert _row(journal, op_id)[:3] == ("COMMITTED", None, None)
    assert _locks(journal, op_id) == 0


@pytest.mark.unit
def test_needs_review_from_prepared_still_works_and_keeps_the_fence(journal):
    op_id = _prepare(journal)
    journal.mark_needs_review(op_id, observed_error="drift", owner_token=oo.process_owner_token())
    assert _row(journal, op_id)[:3] == ("NEEDS_REVIEW", None, "drift")
    assert _locks(journal, op_id) == 2


@pytest.mark.unit
def test_ownerless_prepared_row_stays_exempt_from_the_token_clause(journal):
    op_id = _prepare(journal)
    with sqlite3.connect(journal.db_path) as conn:
        conn.execute(
            "UPDATE graph_operations SET owner_token = NULL, owner_heartbeat_at = NULL, "
            "owner_lease_expires_at = NULL WHERE op_id = ?",
            (op_id,),
        )
        conn.commit()
    journal.mark_committed(op_id, owner_token="anyone")
    assert _row(journal, op_id)[0] == "COMMITTED"


@pytest.mark.unit
def test_clear_needs_review_to_needs_review_is_a_noop(journal):
    op_id = _prepare(journal)
    journal.mark_needs_review(op_id, observed_error="drift", owner_token=oo.process_owner_token())
    with sqlite3.connect(journal.db_path) as conn:
        before = conn.execute(
            "SELECT updated_at FROM graph_operations WHERE op_id = ?", (op_id,)
        ).fetchone()
    journal.clear_needs_review(op_id, to_state="NEEDS_REVIEW")
    with sqlite3.connect(journal.db_path) as conn:
        after = conn.execute(
            "SELECT updated_at FROM graph_operations WHERE op_id = ?", (op_id,)
        ).fetchone()
    assert before == after
    assert _row(journal, op_id)[0] == "NEEDS_REVIEW"
    assert _locks(journal, op_id) == 2


# ------------------------------------------------------------------ F4 record_attempt
@pytest.mark.unit
def test_s5_record_attempt_does_not_overwrite_a_needs_review_row(journal):
    op_id = _prepare(journal)
    journal.mark_needs_review(op_id, observed_error="R evidence", owner_token=oo.process_owner_token())
    assert journal.record_attempt(op_id, error="W stale exc") is False
    state, _o, last_error, attempts = _row(journal, op_id)
    assert (state, last_error, attempts) == ("NEEDS_REVIEW", "R evidence", 0)


@pytest.mark.unit
def test_s5b_record_attempt_does_not_touch_a_committed_row(journal):
    op_id = _prepare(journal)
    journal.mark_committed(op_id, owner_token=oo.process_owner_token())
    assert journal.record_attempt(op_id, error="W stale exc") is False
    assert _row(journal, op_id)[2:] == (None, 0)


@pytest.mark.unit
def test_record_attempt_wrong_owner_is_a_noop_and_right_owner_records(journal):
    op_id = _prepare(journal)
    _real_takeover(journal, op_id)
    assert journal.record_attempt(op_id, error="stale", owner_token=oo.process_owner_token()) is False
    assert _row(journal, op_id)[2:] == (None, 0)
    assert journal.record_attempt(op_id, error="mine", owner_token=_R_TOKEN) is True
    assert _row(journal, op_id)[2:] == ("mine", 1)


@pytest.mark.unit
def test_record_attempt_unknown_op_still_raises(journal):
    with pytest.raises(GraphOperationError):
        journal.record_attempt("missing", error="x")


@pytest.mark.unit
def test_coordinator_reraises_original_error_when_record_attempt_is_a_noop(journal):
    """A stale writer's mutation error must surface as itself, not crash on the journal no-op."""
    op_id = _prepare(journal)
    journal.mark_committed(op_id, owner_token=oo.process_owner_token())
    coord = MergeCoordinator(graph_adapter=MagicMock(), journal=journal)
    coord.graph_adapter.merge_entity.side_effect = RuntimeError("neo4j went away")
    coord._classify_replay = lambda request: (
        "WOULD_REPLAY", {"expected_before": "b", "expected_after": "a"}
    )
    with pytest.raises(RuntimeError, match="neo4j went away"):
        coord._apply_owned(_request(op_id))
    assert _row(journal, op_id)[2:] == (None, 0)


# ------------------------------------------------------------------ F6 dispatcher
def _abandoned_row(journal):
    # A foreign (dead) writer's row, so the dispatcher's process-token claim is a real takeover.
    op_id = journal.prepare(
        operation_kind="ENTITY_MERGE",
        request_json=json.dumps({"absorbed_uuid": "abs", "survivor_uuid": "sur"}),
        owner_token="instance-W:other-host:1:wwww",
    )
    _expire(journal, op_id)
    assert journal.attest_owner_death(op_id, attested_by="operator") is True
    return op_id, journal.get(op_id)


@pytest.mark.unit
def test_dispatcher_reports_a_stale_replay_as_stale_not_as_unresolved(journal):
    op_id, row = _abandoned_row(journal)

    def replay(_row):
        raise StaleTransitionError("moved under us")

    dispatcher = SagaReconcileDispatcher(
        journal=journal, handlers={"ENTITY_MERGE": SimpleNamespace(replay_prepared_row=replay)}
    )
    outcome, diag = dispatcher._replay_one(row, op_id=op_id, kind="ENTITY_MERGE")
    assert outcome == LIVE_OWNER
    assert diag["stale"] is True and diag["observed_error"].startswith("stale transition")


@pytest.mark.unit
def test_dispatcher_stale_needs_review_on_non_replayable_kind_does_not_end_the_pass(journal):
    op_id, row = _abandoned_row(journal)

    class Proxy:
        """Real journal, except the row is committed by someone else right after our claim."""

        def __init__(self, inner):
            self._inner = inner

        def claim_abandoned_operation(self, op, **kw):
            claimed = self._inner.claim_abandoned_operation(op, **kw)
            self._inner.mark_committed(op, owner_token=oo.process_owner_token())
            return claimed

        def __getattr__(self, name):
            return getattr(self._inner, name)

    dispatcher = SagaReconcileDispatcher(
        journal=Proxy(journal),
        handlers={"ENTITY_MERGE": NonReplayableKind(kind="ENTITY_MERGE", reason="no replay")},
    )
    outcome, diag = dispatcher._replay_one(row, op_id=op_id, kind="ENTITY_MERGE")  # must not raise
    assert outcome == LIVE_OWNER and diag["stale"] is True
    assert _row(journal, op_id)[0] == "COMMITTED"
