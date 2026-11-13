"""S6 counterexamples for #427 / #404 / #406 (plan menhir-427-saga-journal-truth, Validation).

Every test drives a REAL coordinator public entry point against a REAL ``GraphOperationsJournal``
(real locks, real states); only the graph adapter is faked. Each docstring names the bad outcome the
test makes impossible.

The #404 stall is simulated concretely, inside the adapter's mutation: the writer's lease is expired
and its death attested, a remote reconciler claims the row and moves it, and only THEN does the
writer's statement land.
"""

from __future__ import annotations

import functools
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from neo4j.exceptions import ConstraintError, ServiceUnavailable

from menhir.domain import merge_delta as md
from menhir.domain import merge_snapshot as ms
from menhir.infrastructure import operation_owner as oo
from menhir.infrastructure.graph_operations import GraphOperationsJournal
from menhir.infrastructure.graph_operations_transitions import (
    REFUSED_FENCE_TAKEN,
    StaleTransitionError,
)
from menhir.infrastructure.metric_receipts import MetricReceiptStore
from menhir.infrastructure.telemetry import recorders
from menhir.infrastructure.telemetry.store import McpTelemetryStore
from menhir.services.delete_coordinator import DeleteCoordinator
from menhir.services.legacy_unmerge_coordinator import LegacyUnmergeCoordinator
from menhir.services.merge_coordinator import MergeCoordinator, MergeDrift
from menhir.services.metric_write_coordinator import MetricWriteCoordinator
from menhir.services.saga_reconcile_dispatcher import SagaReconcileDispatcher, build_handlers
from menhir.services.unmerge_coordinator import UnmergeCoordinator

pytestmark = pytest.mark.unit

#: A token another process would have minted: different host, so this process never claims it as
#: its own and its death can only be proven by attestation.
_REMOTE = "instance-unset:otherhost:4242:0123456789abcdef0123456789abcdef"

_BOTH_PRESENT = {"survivor_present": True, "absorbed_present": True, "lineage_recorded": False}
_ABSORBED_GONE = {"survivor_present": True, "absorbed_present": False, "lineage_recorded": True}


# --------------------------------------------------------------------------- journal helpers
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


def _op_ids(journal, kind: str) -> list[str]:
    with sqlite3.connect(journal.db_path) as conn:
        return [r[0] for r in conn.execute(
            "SELECT op_id FROM graph_operations WHERE operation_kind = ? ORDER BY created_at, rowid",
            (kind,),
        )]


def _only_op(journal, kind: str) -> str:
    ids = _op_ids(journal, kind)
    assert len(ids) == 1, f"expected exactly one {kind} row, got {ids}"
    return ids[0]


def _expire_and_attest(journal, op_id: str) -> None:
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    with sqlite3.connect(journal.db_path) as conn:
        conn.execute(
            "UPDATE graph_operations SET owner_lease_expires_at = ? WHERE op_id = ?",
            (past, op_id),
        )
    assert journal.attest_owner_death(op_id, attested_by="test operator")


# --------------------------------------------------------------------------- fake graph (merge family)
class _Graph:
    """Merge / unmerge / legacy-unmerge adapter. Tests script it; the coordinators do the rest."""

    def __init__(self, state=None):
        self.state = dict(state or _BOTH_PRESENT)
        self.stamp: str | None = None
        self.merge_calls = 0
        self.restore_calls = 0
        self.before_landing = None  # callable(op_id): runs INSIDE the mutation, before it lands
        self.raises: BaseException | None = None  # raised by merge_entity
        self.restore_raises: BaseException | None = None  # raised by restore_merge_snapshot
        self.abstain_reason: str | None = None  # merge_entity returns merged=0 without landing
        self.unreadable = False  # fetch_merge_state raises (the before-state re-read fails)

    # ---- read surface
    def evaluate_merge_eligibility(self, survivor_uuid, absorbed_uuid):
        return SimpleNamespace(allowed=True, reason_code=None, diagnostics={})

    def capture_merge_snapshot(self, survivor_uuid, absorbed_uuid, *, similarity=None):
        return ms.build_snapshot(
            survivor=ms.encode_node(uuid=survivor_uuid, labels=["Entity"], properties={},
                                    relationships=[]),
            absorbed=ms.encode_node(uuid=absorbed_uuid, labels=["Entity"], properties={},
                                    relationships=[]),
            similarity=similarity,
        )

    def fetch_merge_state(self, survivor_uuid, absorbed_uuid):
        if self.unreadable:
            raise RuntimeError("graph unreadable")
        # Like the real read (correlation_queries), report the survivor's op stamp alongside.
        return {**self.state, "last_merge_op_id": self.stamp}

    def fetch_survivor_properties(self, survivor_uuid):
        return md.replay_survivor_merge({}, {})

    def peers_exist(self, uuids):
        return set(uuids)

    # ---- mutations
    def merge_entity(self, survivor_uuid, absorbed_uuid, *, similarity, operation_id=None,
                     expected_fact_uuids=None):
        self.merge_calls += 1
        if self.before_landing is not None:
            self.before_landing(operation_id)
        if self.raises is not None:
            raise self.raises
        if self.abstain_reason is not None:
            return {"merged": 0, "reason": self.abstain_reason}
        self.state = dict(_ABSORBED_GONE)
        self.stamp = operation_id
        return {"merged": 1}

    def restore_merge_snapshot(self, **kw):
        self.restore_calls += 1
        if self.restore_raises is not None:
            raise self.restore_raises
        self.state = dict(_BOTH_PRESENT)
        return {"restored": 1}


def _merge(coord: MergeCoordinator) -> dict:
    return coord.merge(survivor_uuid="s", absorbed_uuid="a", similarity=0.9)


def _reconciler_takes_over(journal, op_id: str) -> str:
    """The writer's lease expires and its death is attested; a remote reconciler claims the row."""
    _expire_and_attest(journal, op_id)
    assert journal.claim_abandoned_operation(op_id, owner_token=_REMOTE)
    return _REMOTE


# =========================================================================== 1. #427 per kind
class _Metric:
    """Metric adapter that mirrors the recorded metric state, with a switchable server error."""

    def __init__(self):
        self.metrics: dict[str, dict] = {}
        self.raises: BaseException | None = None

    def record_metric(self, *, subject, counter, value, receipt_op_id, namespace=None,
                      valid_at=None, source="instrumentation", node_uuid=None, **_):
        if self.raises is not None:
            raise self.raises
        key = MetricWriteCoordinator.view_key(namespace, subject, counter)
        self.metrics[key] = {"uuid": node_uuid, "value": float(value),
                             "receipt_op_id": receipt_op_id}
        return {"uuid": node_uuid, "view_key": key, "created": True}

    def fetch_metric(self, *, subject, counter, namespace=None):
        return None

    def fetch_metric_state(self, *, view_key):
        got = self.metrics.get(view_key)
        if not got:
            return None
        return {"uuid": got["uuid"], "value": got["value"], "type": "METRIC",
                "labels": ["Metric"], "view_current": True, "receipt_op_id": got["receipt_op_id"]}


class _Nodes:
    """Delete adapter: DETACH DELETE is one statement, so a server rollback leaves every node."""

    def __init__(self):
        self.present = {"n1"}
        self.raises: BaseException | None = None

    def capture_node_state(self, uuid):
        return {"uuid": uuid} if uuid in self.present else None

    def newly_unreferenced_evidence(self, uuids):
        return []

    def delete_entities_returning_uuids(self, uuids, *, require_scope=None, protect_retention=False):
        if self.raises is not None:
            raise self.raises
        self.present -= set(uuids)
        return list(uuids)


class _LegacyAudit:
    def fetch_merge_audit(self, *, absorbed_uuid, limit=1):
        return [{"snapshot_json": json.dumps({
            "survivor_uuid": "s", "absorbed_uuid": absorbed_uuid,
            "properties": {"uuid": absorbed_uuid, "name": "absorbed"},
            "relationships": [], "absorbed_episodes": [], "rebound_episodes": [],
        })}]


def _scenario(kind: str, journal, tmp_path):
    """Returns (operation_kind, run, clear_error): run() hits the public entry point and the
    mutation fails; clear_error() removes the fault so the same call can be retried."""
    if kind == "merge":
        graph = _Graph()
        coord = MergeCoordinator(graph_adapter=graph, journal=journal)
        graph.raises = ConstraintError("race: node gained a relationship")
        return "ENTITY_MERGE", (lambda: _merge(coord)), (lambda: setattr(graph, "raises", None))
    if kind == "unmerge":
        graph = _Graph()
        merged = _merge(MergeCoordinator(graph_adapter=graph, journal=journal))
        assert merged["merged"] == 1
        coord = UnmergeCoordinator(graph_adapter=graph, journal=journal)
        graph.restore_raises = ConstraintError("race")
        return (
            "ENTITY_UNMERGE",
            (lambda: coord.unmerge(merged["op_id"])),
            (lambda: setattr(graph, "restore_raises", None)),
        )
    if kind == "legacy_unmerge":
        graph = _Graph(_ABSORBED_GONE)
        coord = LegacyUnmergeCoordinator(
            graph_adapter=graph, journal=journal, telemetry_store=_LegacyAudit()
        )
        graph.restore_raises = ConstraintError("race")
        return (
            "LEGACY_ENTITY_UNMERGE",
            (lambda: coord.unmerge_legacy("a", manifest=["a"], acknowledge_degraded=True)),
            (lambda: setattr(graph, "restore_raises", None)),
        )
    if kind == "metric":
        graph = _Metric()
        coord = MetricWriteCoordinator(
            graph_adapter=graph, journal=journal,
            receipts=MetricReceiptStore(db_path=journal.db_path),
            telemetry_db_path=journal.db_path,
        )
        graph.raises = ConstraintError("metric_uuid_unique")
        return (
            "METRIC_WRITE",
            (lambda: coord.record_run_tally(subject="perception", counter="abstained", value=3.0,
                                            namespace="ns")),
            (lambda: setattr(graph, "raises", None)),
        )
    if kind == "delete":
        graph = _Nodes()
        coord = DeleteCoordinator(graph_adapter=graph, journal=journal)
        graph.raises = ConstraintError("node still has relationships")
        return "ENTITY_DELETE", (lambda: coord.delete_entity("n1")), (
            lambda: setattr(graph, "raises", None))
    raise AssertionError(kind)


@pytest.mark.parametrize("kind", ["merge", "unmerge", "metric", "legacy_unmerge", "delete"])
def test_server_rollback_with_graph_at_before_state_closes_failed_and_releases_the_fence(
    journal, tmp_path, kind
):
    """#427: a provable rollback must not leave a PREPARED row holding locks (boot-blocking)."""
    op_kind, run, clear_error = _scenario(kind, journal, tmp_path)

    with pytest.raises(ConstraintError):
        run()

    op_id = _op_ids(journal, op_kind)[-1]
    row = _row(journal, op_id)
    assert row["state"] == "FAILED"
    assert row["attempt_count"] == 1
    assert _locks(journal, op_id) == set()
    assert journal.list_by_state("PREPARED") == []

    # Not fenced: the same call prepares and completes once the fault is gone.
    clear_error()
    run()
    assert len(_op_ids(journal, op_kind)) == 2
    assert _row(journal, _op_ids(journal, op_kind)[-1])["state"] == "COMMITTED"


def test_second_merge_of_the_same_pair_is_not_fenced_after_a_rolled_back_merge(journal):
    """#427: a rolled-back merge must not make the pair unmergeable (PREPARE_FAILED forever)."""
    graph = _Graph()
    coord = MergeCoordinator(graph_adapter=graph, journal=journal)
    graph.raises = ConstraintError("race")
    with pytest.raises(ConstraintError):
        _merge(coord)

    graph.raises = None
    second = _merge(coord)

    assert second.get("reason") != "PREPARE_FAILED"
    assert second["merged"] == 1
    assert graph.merge_calls == 2


# =========================================================================== 2. ambiguity stays PREPARED
def test_connection_class_error_leaves_the_merge_prepared_with_its_locks(journal):
    """#427 converse: FAILED after a ServiceUnavailable would release the fence over a commit that
    may have landed after the socket dropped."""
    graph = _Graph()
    graph.raises = ServiceUnavailable("socket closed")
    coord = MergeCoordinator(graph_adapter=graph, journal=journal)

    with pytest.raises(ServiceUnavailable):
        _merge(coord)

    op_id = _only_op(journal, "ENTITY_MERGE")
    row = _row(journal, op_id)
    assert row["state"] == "PREPARED"
    assert row["attempt_count"] == 1
    assert _locks(journal, op_id) == {"s", "a"}
    # and the pair really is fenced
    assert _merge(coord).get("reason") == "PREPARE_FAILED"


def test_rollback_with_a_graph_that_moved_off_the_before_state_stays_prepared(journal):
    """#427: ConstraintError is not proof of 'no mutation' unless the graph is re-read at the
    before-state; someone else's write must keep the row PREPARED for a reconciler."""
    graph = _Graph()

    def _someone_else_wrote(op_id):
        graph.state = {"survivor_present": True, "absorbed_present": True, "lineage_recorded": True}

    graph.before_landing = _someone_else_wrote
    graph.raises = ConstraintError("race")
    coord = MergeCoordinator(graph_adapter=graph, journal=journal)

    with pytest.raises(ConstraintError):
        _merge(coord)

    op_id = _only_op(journal, "ENTITY_MERGE")
    assert _row(journal, op_id)["state"] == "PREPARED"
    assert _locks(journal, op_id) == {"s", "a"}


def test_unreadable_graph_after_a_rollback_stays_prepared(journal):
    """#427: a failed before-state re-read proves nothing; FAILED would be a guess."""
    graph = _Graph()

    def _graph_goes_dark(op_id):
        graph.unreadable = True

    graph.before_landing = _graph_goes_dark
    graph.raises = ConstraintError("race")
    coord = MergeCoordinator(graph_adapter=graph, journal=journal)

    with pytest.raises(ConstraintError):
        _merge(coord)

    op_id = _only_op(journal, "ENTITY_MERGE")
    assert _row(journal, op_id)["state"] == "PREPARED"
    assert _locks(journal, op_id) == {"s", "a"}


# =========================================================================== 3. live boot
class _Gate:
    lease_duration_s = 3600.0

    def verify_still_held(self):
        pass

    def renew(self):
        return True


def _live_pass(journal, coord):
    return SagaReconcileDispatcher(
        journal=journal, handlers=build_handlers(merge=coord)
    ).run(dry_run=False, gate=_Gate())


def test_live_boot_is_write_ready_after_a_rolled_back_merge(journal):
    """#427: a stranded PREPARED row from a provable rollback must not block `live` boot."""
    graph = _Graph()
    graph.raises = ConstraintError("race")
    coord = MergeCoordinator(graph_adapter=graph, journal=journal)
    with pytest.raises(ConstraintError):
        _merge(coord)

    run = _live_pass(journal, coord)

    assert run.write_ready is True, run.blocking_reasons
    assert run.blocking_reasons == []


def test_live_boot_is_not_write_ready_after_an_ambiguous_merge_failure(journal):
    """#427 converse: an ambiguous error leaves a PREPARED row, and a row with an unattested owner
    must keep blocking boot -- the fix must not simply make boot always succeed."""
    graph = _Graph()
    graph.raises = ServiceUnavailable("socket closed")
    coord = MergeCoordinator(graph_adapter=graph, journal=journal)
    with pytest.raises(ServiceUnavailable):
        _merge(coord)

    run = _live_pass(journal, coord)

    assert run.write_ready is False
    assert run.blocking_reasons


# =========================================================================== 4. #404 stall
def test_writer_landing_after_reconciler_failed_the_row_escalates_to_needs_review(journal):
    """#404: a writer whose statement lands after a reconciler wrote FAILED must not leave FAILED
    (fence released) over a mutated graph."""
    graph = _Graph()
    coord = MergeCoordinator(graph_adapter=graph, journal=journal)

    def _stall(op_id):
        _reconciler_takes_over(journal, op_id)
        # The reconciler re-reads the graph: still at the before-state, so FAILED is, for now, true.
        assert graph.state == _BOTH_PRESENT
        journal.mark_failed(op_id, owner_token=_REMOTE, reason="reconciler: graph at before-state")
        assert _locks(journal, op_id) == set()

    graph.before_landing = _stall

    with pytest.raises(StaleTransitionError):
        _merge(coord)

    op_id = _only_op(journal, "ENTITY_MERGE")
    assert graph.stamp == op_id and graph.state == _ABSORBED_GONE  # the write did land
    row = _row(journal, op_id)
    assert row["state"] == "NEEDS_REVIEW"
    assert _locks(journal, op_id) == {"s", "a"}


def test_escalation_is_refused_and_recorded_when_another_op_took_a_participant(journal, store):
    """#404: re-fencing must not steal a participant a newer op now holds; the refusal must be
    recorded rather than silently leaving FAILED over a mutated graph."""
    graph = _Graph()
    coord = MergeCoordinator(graph_adapter=graph, journal=journal)
    other: dict[str, str] = {}

    def _stall(op_id):
        _reconciler_takes_over(journal, op_id)
        journal.mark_failed(op_id, owner_token=_REMOTE, reason="reconciler: graph at before-state")
        other["op"] = journal.prepare(
            operation_kind="ENTITY_MERGE",
            request_json=json.dumps({"survivor_uuid": "s", "absorbed_uuid": "third"}),
            target_key="s|third",
            owner_token="owner-X",
        )

    graph.before_landing = _stall

    with pytest.raises(StaleTransitionError):
        _merge(coord)

    op_id = _op_ids(journal, "ENTITY_MERGE")[0]
    assert _row(journal, op_id)["state"] == "FAILED"
    assert _locks(journal, op_id) == set()
    assert _locks(journal, other["op"]) == {"s", "third"}
    [event] = _events(store, "commit_refused_escalation")
    assert event["escalation"] == REFUSED_FENCE_TAKEN
    assert event["op_id"] == op_id


# =========================================================================== 5. COMMITTED by another owner
def test_writer_succeeds_when_the_reconciler_only_observed_and_committed(journal):
    """#404 FR-6: one verified application must be reported as success, not as a stale-commit
    failure (which would also hide the merge from the merge hook)."""
    graph = _Graph()
    coord = MergeCoordinator(graph_adapter=graph, journal=journal)

    def _stall(op_id):
        _reconciler_takes_over(journal, op_id)
        # The reconciler only observed (after_graph_work=False); the writer's statement lands next.
        journal.mark_committed(op_id, owner_token=_REMOTE, after_graph_work=False)

    graph.before_landing = _stall

    result = _merge(coord)

    assert result["merged"]
    assert result["completed_by_other_owner"] is True
    op_id = _only_op(journal, "ENTITY_MERGE")
    row = _row(journal, op_id)
    assert row["state"] == "COMMITTED"
    assert row["resolved_by_token"] == _REMOTE


def test_writer_escalates_when_the_reconciler_also_did_graph_work(journal):
    """#404: two owners that both applied the write is a possible double apply; reporting success
    would hide it."""
    graph = _Graph()
    coord = MergeCoordinator(graph_adapter=graph, journal=journal)

    def _stall(op_id):
        _reconciler_takes_over(journal, op_id)
        journal.mark_committed(op_id, owner_token=_REMOTE, after_graph_work=True)

    graph.before_landing = _stall

    with pytest.raises(StaleTransitionError):
        _merge(coord)

    op_id = _only_op(journal, "ENTITY_MERGE")
    assert _row(journal, op_id)["state"] == "NEEDS_REVIEW"
    assert _locks(journal, op_id) == {"s", "a"}


# =========================================================================== 6. FR-5 abstain
def test_abstaining_writer_reports_success_when_a_reconciler_already_committed_its_after_state(
    journal,
):
    """#404 FR-5: a writer whose statement no-oped because a reconciler already applied and
    COMMITTED this op must report merged=1 (once), not raise or report an abstention."""
    graph = _Graph()
    coord = MergeCoordinator(graph_adapter=graph, journal=journal)

    def _reconciler_applies_and_commits(op_id):
        _reconciler_takes_over(journal, op_id)
        graph.state = dict(_ABSORBED_GONE)
        graph.stamp = op_id
        journal.mark_committed(op_id, owner_token=_REMOTE, after_graph_work=True)

    graph.before_landing = _reconciler_applies_and_commits
    graph.abstain_reason = "ALREADY_MERGED"

    result = _merge(coord)

    assert result["merged"] == 1
    assert result["completed_by_other_owner"] is True
    op_id = _only_op(journal, "ENTITY_MERGE")
    assert _row(journal, op_id)["state"] == "COMMITTED"
    assert _row(journal, op_id)["resolved_by_token"] == _REMOTE


@pytest.mark.unit
def test_abstention_at_the_after_state_of_another_merge_is_not_reported_as_this_merge(journal):
    """Review F1: lineage left by an older merge plus an absorbed node deleted elsewhere matches the
    after-state fingerprint; without this op's stamp the writer must quarantine, not report merged."""
    graph = _Graph()
    coord = MergeCoordinator(graph_adapter=graph, journal=journal)

    def _someone_else_absorbed_it(op_id):
        graph.state = dict(_ABSORBED_GONE)
        graph.stamp = "an-older-merge"

    graph.before_landing = _someone_else_absorbed_it
    graph.abstain_reason = "ABSORBED_MISSING"

    with pytest.raises(MergeDrift):
        _merge(coord)
    op_id = _only_op(journal, "ENTITY_MERGE")
    assert _row(journal, op_id)["state"] == "NEEDS_REVIEW"


# =========================================================================== 7. F-10 token identity
class _RecordingJournal(GraphOperationsJournal):
    """A real journal that records the owner token of every mark/renew it is handed."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.seen: list[tuple[str, str]] = []

    def mark_committed(self, op_id, *, owner_token, **kw):
        self.seen.append(("mark_committed", owner_token))
        return super().mark_committed(op_id, owner_token=owner_token, **kw)

    def mark_failed(self, op_id, *, owner_token, **kw):
        self.seen.append(("mark_failed", owner_token))
        return super().mark_failed(op_id, owner_token=owner_token, **kw)

    def mark_needs_review(self, op_id, *, owner_token, **kw):
        self.seen.append(("mark_needs_review", owner_token))
        return super().mark_needs_review(op_id, owner_token=owner_token, **kw)

    def record_attempt(self, op_id, *, owner_token, **kw):
        self.seen.append(("record_attempt", owner_token))
        return super().record_attempt(op_id, owner_token=owner_token, **kw)

    def renew_owner_heartbeat(self, op_id, *, owner_token=None, **kw):
        self.seen.append(("renew_owner_heartbeat", owner_token))
        return super().renew_owner_heartbeat(op_id, owner_token=owner_token, **kw)


@pytest.mark.parametrize("outcome", ["commit", "ambiguous_failure"])
def test_every_token_the_journal_sees_is_the_claim_token_prepare_stored(tmp_path, outcome):
    """#404 F-3 / F-10: a mark made under the bare process token (or any token other than the one
    PREPARE stored) would re-validate a stale writer after a P -> R -> P takeover."""
    journal = _RecordingJournal(db_path=tmp_path / "ops.db")
    graph = _Graph()
    if outcome == "ambiguous_failure":
        graph.raises = ServiceUnavailable("socket closed")
    coord = MergeCoordinator(graph_adapter=graph, journal=journal)

    if outcome == "commit":
        _merge(coord)
    else:
        with pytest.raises(ServiceUnavailable):
            _merge(coord)

    op_id = _only_op(journal, "ENTITY_MERGE")
    claims = _claims(journal, op_id)
    assert [claim_kind for _, claim_kind in claims] == ["prepare"]
    stored = claims[0][0]  # the token PREPARE stored (the row's owner_token is cleared on COMMIT)
    assert journal.seen, "the writer made no journal mark at all"
    assert {tok for _, tok in journal.seen} == {stored}, journal.seen
    row = _row(journal, op_id)
    if outcome == "commit":
        assert row["state"] == "COMMITTED" and row["resolved_by_token"] == stored
    else:
        assert row["state"] == "PREPARED" and row["owner_token"] == stored
    # the token is a per-claim token, not the bare process token, but still this process's
    assert stored != oo.process_owner_token()
    assert oo.same_process(stored, process_token=oo.process_owner_token())
