"""CF-20a: ``reconcile(dry_run=True)`` is a genuinely non-mutating observation contract.

These tests use a REAL ``GraphOperationsJournal`` on a temp SQLite file and compare a full dump
of both saga tables before and after each dry-run. That is deliberate. Asserting "``mark_committed``
was not called" on a stub journal only proves the call the test author thought of did not happen;
it says nothing about ``record_attempt``, an ``attempt_count`` bump, a released participant lock, or
an ``updated_at`` touch. The plan is explicit that method-call mocks are insufficient here, so the
oracle is the durable state itself: every column of every row in ``graph_operations`` and
``graph_operation_locks``.

The graph adapters remain stubs -- there is no live Neo4j in unit tests, and the graph is not the
thing whose immutability is in question. The journal is.

A mutation-negative test passes trivially if ``reconcile`` does nothing at all, so each
non-mutation assertion is paired with a live-mode control on the SAME fixture proving the
tables DO change when ``dry_run=False``. That is what makes the flag, and not a broken
reconciler, the cause of the silence.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from menhir.domain import merge_snapshot as ms
from menhir.infrastructure.graph_operations import GraphOperationsJournal
from menhir.infrastructure.metric_receipts import MetricReceiptStore
from menhir.services.delete_coordinator import DeleteCoordinator
from menhir.services.merge_coordinator import MergeCoordinator, merge_state_fingerprint
from menhir.services.metric_write_coordinator import (
    MetricWriteCoordinator,
    state_fingerprint,
)
from menhir.services.saga_reconcile_outcomes import (
    ALL_OUTCOMES,
    CF20A_REACHABLE_OUTCOMES,
    SKIP,
    WOULD_MARK_ALREADY_APPLIED,
    WOULD_NEEDS_REVIEW,
    WOULD_REPLAY,
    WOULD_RESTORE,
    summarize_outcomes,
)
from menhir.services.unmerge_coordinator import UnmergeCoordinator

_MERGED = {"survivor_present": True, "absorbed_present": False, "lineage_recorded": True}
_DRIFTED = {"survivor_present": True, "absorbed_present": True, "lineage_recorded": True}


# --------------------------------------------------------------------------- durable-state oracle


def _dump(db_path) -> tuple[list, list]:
    """Every column of every row in both saga tables, ordered deterministically.

    Comparing full rows (not just ``state``) is the point: it catches an attempt_count
    increment, a last_error write, an updated_at touch, and a dropped participant lock --
    all of which are durable mutations a dry-run must not perform.
    """
    with sqlite3.connect(db_path) as conn:
        ops = [
            tuple(r)
            for r in conn.execute("SELECT * FROM graph_operations ORDER BY op_id")
        ]
        locks = [
            tuple(r)
            for r in conn.execute(
                "SELECT * FROM graph_operation_locks ORDER BY entity_uuid"
            )
        ]
    return ops, locks


# --------------------------------------------------------------------------- graph stubs


class _MergeAdapter:
    """Graph state is whatever the test says it is; records any mutation attempt."""

    def __init__(self, state):
        self.state = dict(state)
        self.mutations = 0

    def fetch_merge_state(self, survivor_uuid, absorbed_uuid):
        return dict(self.state)

    def merge_entity(self, *a, **k):
        self.mutations += 1
        return {"merged": 1}

    def restore_merge_snapshot(self, **k):
        self.mutations += 1
        return {"restored": 1}


class _DeleteAdapter:
    """``capture_node_state`` returns None for uuids the test declares already gone."""

    def __init__(self, present=()):
        self.present = set(present)

    def capture_node_state(self, uuid):
        return {"uuid": uuid} if uuid in self.present else None


class _MetricAdapter:
    def __init__(self, state=None):
        self.state = state
        self.mutations = 0

    def fetch_metric_state(self, *, view_key):
        return self.state

    def record_metric(self, **k):
        self.mutations += 1
        return {"uuid": k.get("node_uuid")}


# --------------------------------------------------------------------------- fixtures


@pytest.fixture()
def journal(tmp_path):
    j = GraphOperationsJournal(db_path=tmp_path / "ops.db")
    j._ensure_ready()
    return j


def _prepare_merge(journal, op_id, *, expected_before, expected_after=None):
    request = {
        "op_id": op_id,
        "survivor_uuid": f"s-{op_id}",
        "absorbed_uuid": f"a-{op_id}",
        "similarity": 0.99,
        "expected_before_sha256": expected_before,
    }
    journal.prepare(
        operation_kind="ENTITY_MERGE",
        request_json=json.dumps(request),
        target_key=f"pair-{op_id}",
        expected_after_sha256=expected_after,
        op_id=op_id,
    )
    return request


def _prepare_delete(journal, op_id, targets):
    request = {"op_id": op_id, "targets": list(targets)}
    journal.prepare(
        operation_kind="ENTITY_DELETE",
        request_json=json.dumps(request),
        target_key=f"del-{op_id}",
        op_id=op_id,
    )
    return request


# --------------------------------------------------------------------------- the core property


@pytest.mark.unit
def test_merge_dry_run_mutates_no_durable_state(journal):
    """A drifted merge row: live mode quarantines it, dry-run must leave it untouched."""
    op_id = "merge-drift"
    expected_before = merge_state_fingerprint(_MERGED, op_id=op_id)
    _prepare_merge(journal, op_id, expected_before=expected_before)
    adapter = _MergeAdapter(_DRIFTED)
    coord = MergeCoordinator(graph_adapter=adapter, journal=journal)

    before = _dump(journal.db_path)
    result = coord.reconcile(dry_run=True)
    after = _dump(journal.db_path)

    assert after == before, "dry-run mutated durable saga state"
    assert adapter.mutations == 0, "dry-run must not touch the graph either"
    assert result["dry_run"] is True
    assert result["counts"][WOULD_NEEDS_REVIEW] == 1
    # The live-action counters describe work performed, and none was.
    assert (result["replayed"], result["drifted"], result["failed"]) == (0, 0, 0)


@pytest.mark.unit
def test_merge_live_mode_does_mutate_the_same_fixture(journal):
    """Control for the test above: without dry_run the identical fixture DOES change.

    Without this, the non-mutation assertion would also pass if reconcile silently did nothing.
    """
    op_id = "merge-drift"
    expected_before = merge_state_fingerprint(_MERGED, op_id=op_id)
    _prepare_merge(journal, op_id, expected_before=expected_before)
    coord = MergeCoordinator(graph_adapter=_MergeAdapter(_DRIFTED), journal=journal)

    before = _dump(journal.db_path)
    coord._replay_prepared()
    after = _dump(journal.db_path)

    assert after != before, "live reconcile of a drifted row must quarantine it"
    assert journal.get(op_id)["state"] == "NEEDS_REVIEW"


@pytest.mark.unit
def test_delete_dry_run_mutates_no_durable_state(journal):
    op_id = "del-gone"
    _prepare_delete(journal, op_id, ["n1", "n2"])
    coord = DeleteCoordinator(graph_adapter=_DeleteAdapter(present=()), journal=journal)

    before = _dump(journal.db_path)
    result = coord.reconcile(dry_run=True)
    after = _dump(journal.db_path)

    assert after == before, "dry-run mutated durable saga state"
    assert result["counts"][WOULD_MARK_ALREADY_APPLIED] == 1
    # These count journal transitions performed; a dry-run performs none.
    assert (result["committed"], result["needs_review"]) == (0, 0)


@pytest.mark.unit
def test_delete_dry_run_prediction_matches_live_outcome(journal):
    """The forecast must not diverge from what live mode then actually does."""
    op_id = "del-gone"
    _prepare_delete(journal, op_id, ["n1", "n2"])
    coord = DeleteCoordinator(graph_adapter=_DeleteAdapter(present=()), journal=journal)

    forecast = coord.reconcile(dry_run=True)
    predicted = forecast["outcomes"][0]["outcome"]
    assert predicted == WOULD_MARK_ALREADY_APPLIED

    coord._replay_prepared()
    assert journal.get(op_id)["state"] == "COMMITTED", (
        "dry-run predicted the row was already applied; live mode must agree"
    )


@pytest.mark.unit
def test_delete_dry_run_reports_survivors_without_quarantining(journal):
    op_id = "del-partial"
    _prepare_delete(journal, op_id, ["n1", "n2"])
    coord = DeleteCoordinator(graph_adapter=_DeleteAdapter(present=("n2",)), journal=journal)

    before = _dump(journal.db_path)
    result = coord.reconcile(dry_run=True)
    after = _dump(journal.db_path)

    assert after == before
    entry = result["outcomes"][0]
    assert entry["outcome"] == WOULD_NEEDS_REVIEW
    assert entry["survivors"] == ["n2"], "the surviving node must be named in the forecast"
    assert journal.get(op_id)["state"] == "PREPARED", "row must still be PREPARED"


@pytest.mark.unit
def test_metric_dry_run_mutates_no_durable_state(journal, tmp_path):
    op_id = "metric-1"
    key = "subject|counter"
    request = {
        "op_id": op_id,
        "view_key": key,
        "subject": "s",
        "counter": "c",
        "value": 1.0,
        "namespace": "ns",
        "valid_at": "2026-01-01T00:00:00+00:00",
        "source": "test",
        "metric_uuid": "m-1",
        "expected_before_sha256": state_fingerprint(None, view_key=key),
    }
    journal.prepare(
        operation_kind="METRIC_WRITE",
        request_json=json.dumps(request),
        target_key=key,
        op_id=op_id,
    )
    adapter = _MetricAdapter(state=None)
    coord = MetricWriteCoordinator(
        graph_adapter=adapter,
        journal=journal,
        receipts=MetricReceiptStore(db_path=tmp_path / "ops.db"),
    )

    before = _dump(journal.db_path)
    result = coord.reconcile(dry_run=True)
    after = _dump(journal.db_path)

    assert after == before, "dry-run mutated durable saga state"
    assert adapter.mutations == 0, "dry-run must never call record_metric"
    # Preconditions hold, so live mode WOULD have replayed -- and that is exactly the
    # case where a leaky dry-run would have written to the graph.
    assert result["counts"][WOULD_REPLAY] == 1


@pytest.mark.unit
def test_unmerge_dry_run_mutates_no_durable_state(journal):
    """An unmerge row with no recovery snapshot: unreplayable, and still not mutated."""
    op_id = "unmerge-1"
    request = {
        "op_id": op_id,
        "survivor_uuid": "s1",
        "absorbed_uuid": "a1",
        "merge_op_id": "m1",
        "expected_before_sha256": "whatever",
    }
    journal.prepare(
        operation_kind="ENTITY_UNMERGE",
        request_json=json.dumps(request),
        target_key="pair-unmerge-1",
        op_id=op_id,
    )
    adapter = _MergeAdapter(_MERGED)
    coord = UnmergeCoordinator(graph_adapter=adapter, journal=journal)

    before = _dump(journal.db_path)
    result = coord.reconcile(dry_run=True)
    after = _dump(journal.db_path)

    assert after == before, "dry-run mutated durable saga state"
    assert adapter.mutations == 0
    assert result["counts"][WOULD_NEEDS_REVIEW] == 1


# --------------------------------------------------------------------------- cross-saga contract


@pytest.mark.unit
def test_unmerge_dry_run_builds_the_restore_plan_and_forecasts_restore(journal):
    """The plan requires a dry-run to prove its pure reads SUCCEED, not just that it wrote nothing.

    The previous test uses a row with no snapshot, which fails before the interesting work. This
    one supplies a valid snapshot so the dry-run must actually load it and build the restore plan
    -- and still reach WOULD_RESTORE without calling restore_merge_snapshot.
    """
    op_id = "unmerge-ok"
    snapshot = ms.build_snapshot(
        survivor=ms.encode_node(uuid="s1", labels=["Entity"], properties={}, relationships=[]),
        absorbed=ms.encode_node(uuid="a1", labels=["Entity"], properties={}, relationships=[]),
        similarity=0.97,
    )
    request = {
        "op_id": op_id,
        "survivor_uuid": "s1",
        "absorbed_uuid": "a1",
        "merge_op_id": "m1",
        "expected_before_sha256": merge_state_fingerprint(_MERGED, op_id=op_id),
    }
    journal.prepare(
        operation_kind="ENTITY_UNMERGE",
        request_json=json.dumps(request),
        target_key="pair-unmerge-ok",
        before_snapshot_json=ms.dumps(snapshot),
        op_id=op_id,
    )
    adapter = _MergeAdapter(_MERGED)
    coord = UnmergeCoordinator(graph_adapter=adapter, journal=journal)

    before = _dump(journal.db_path)
    result = coord.reconcile(dry_run=True)
    after = _dump(journal.db_path)

    assert after == before, "dry-run mutated durable saga state"
    assert adapter.mutations == 0, "dry-run must never call restore_merge_snapshot"
    assert result["counts"][WOULD_RESTORE] == 1, (
        "a restorable row must forecast WOULD_RESTORE, proving the snapshot loaded and the "
        "restore plan was built"
    )


@pytest.mark.unit
def test_foreign_saga_kinds_are_reported_as_skip_not_silently_dropped(journal):
    """Each reconciler sees other coordinators' PREPARED rows; a dry-run must account for them."""
    _prepare_delete(journal, "del-1", ["n1"])
    op_id = "merge-1"
    _prepare_merge(journal, op_id, expected_before=merge_state_fingerprint(_MERGED, op_id=op_id))

    coord = DeleteCoordinator(graph_adapter=_DeleteAdapter(present=()), journal=journal)
    result = coord.reconcile(dry_run=True)

    assert result["scanned"] == 2, "both PREPARED rows must be scanned"
    assert result["counts"][SKIP] == 1, "the merge row must be reported, not silently skipped"
    kinds = {e["operation_kind"] for e in result["outcomes"]}
    assert "ENTITY_MERGE" in kinds


@pytest.mark.unit
def test_unclassifiable_old_row_does_not_hide_newer_rows(journal):
    """Hazard 1: a permanently bad old row must not stop the scan reaching newer rows.

    ``_classify_replay`` reads request fields directly, so a row missing one raises instead of
    returning an outcome. Ordered oldest-first, an unhandled raise on the first row would take
    the whole preflight down and hide every row behind it -- the exact starvation the exhaustive
    scan exists to prevent. The bad row must be reported and the scan must continue.
    """
    # Oldest row first: valid JSON, but no survivor_uuid for the classifier to read.
    journal.prepare(
        operation_kind="ENTITY_MERGE",
        request_json=json.dumps({"op_id": "merge-bad", "absorbed_uuid": "a"}),
        target_key="pair-bad",
        op_id="merge-bad",
    )
    good = "merge-good"
    _prepare_merge(journal, good, expected_before=merge_state_fingerprint(_MERGED, op_id=good))

    coord = MergeCoordinator(graph_adapter=_MergeAdapter(_DRIFTED), journal=journal)
    before = _dump(journal.db_path)
    result = coord.reconcile(dry_run=True)
    after = _dump(journal.db_path)

    assert after == before, "dry-run must still mutate nothing on the error path"
    assert result["scanned"] == 2
    by_id = {e["op_id"]: e for e in result["outcomes"]}
    assert by_id["merge-bad"]["outcome"] == WOULD_NEEDS_REVIEW
    assert "unclassifiable" in by_id["merge-bad"]["observed_error"]
    assert good in by_id, "the newer row must still have been reached and classified"
    assert by_id[good]["outcome"] == WOULD_NEEDS_REVIEW  # drifted graph


@pytest.mark.unit
def test_scanned_equals_total_classified_rows(journal):
    """Every scanned row gets exactly one outcome -- no row falls through unclassified."""
    _prepare_delete(journal, "del-1", ["n1"])
    _prepare_delete(journal, "del-2", ["n2"])
    op_id = "merge-1"
    _prepare_merge(journal, op_id, expected_before=merge_state_fingerprint(_MERGED, op_id=op_id))

    coord = DeleteCoordinator(graph_adapter=_DeleteAdapter(present=()), journal=journal)
    result = coord.reconcile(dry_run=True)

    assert sum(result["counts"].values()) == result["scanned"] == 3


@pytest.mark.unit
def test_live_mode_return_shape_gains_no_new_keys(journal):
    """Existing callers must not see dry-run bookkeeping leak into live results."""
    _prepare_delete(journal, "del-1", ["n1"])
    delete_coord = DeleteCoordinator(graph_adapter=_DeleteAdapter(present=()), journal=journal)
    assert set(delete_coord._replay_prepared().keys()) == {"committed", "needs_review"}

    op_id = "merge-1"
    _prepare_merge(journal, op_id, expected_before=merge_state_fingerprint(_MERGED, op_id=op_id))
    merge_coord = MergeCoordinator(graph_adapter=_MergeAdapter(_DRIFTED), journal=journal)
    assert set(merge_coord._replay_prepared().keys()) == {"replayed", "drifted", "failed"}


# --------------------------------------------------------------------------- vocabulary


@pytest.mark.unit
def test_vocabulary_has_no_would_commit():
    """The plan bans WOULD_COMMIT by name: a dry-run proves the decision, not the commit."""
    assert "WOULD_COMMIT" not in ALL_OUTCOMES
    assert not any(name.endswith("COMMIT") for name in ALL_OUTCOMES)


@pytest.mark.unit
def test_ownership_outcomes_are_not_reachable_in_cf20a():
    """LIVE_OWNER / OWNER_UNKNOWN need CF-20b metadata; a 20a summary must not imply otherwise."""
    counts = summarize_outcomes([])
    assert "LIVE_OWNER" not in counts
    assert "OWNER_UNKNOWN" not in counts
    assert set(counts) == set(CF20A_REACHABLE_OUTCOMES)


@pytest.mark.unit
def test_summarize_counts_unknown_outcome_rather_than_dropping_it():
    """A reconciler emitting something outside the vocabulary is a defect that must be visible."""
    counts = summarize_outcomes([{"outcome": "SOMETHING_NEW"}])
    assert counts["SOMETHING_NEW"] == 1
