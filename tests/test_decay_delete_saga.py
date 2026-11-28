"""DECAY_DELETE: the decay sweep's physical delete runs through the journaled DeleteCoordinator.

Offline. A real journal over a fake graph adapter; only the graph is faked. The Cypher predicates
and bridge rules are covered online in ``test_decay_delete_bridges_live.py``.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from unittest.mock import MagicMock, patch

import pytest

from menhir.config import MemorySettings
from menhir.infrastructure import process_liveness
from menhir.infrastructure.graph_operations import GraphOperationsJournal
from menhir.infrastructure.pending_actions import PendingActionStore
from menhir.services.delete_coordinator import (
    GATE_REFUSED,
    MISSING_READ_VALUES,
    POLICY_DECLINED,
    DeleteCoordinator,
)
from menhir.services.lifecycle_models import (
    _DEFAULT_GONE_DAYS,
    _DEFAULT_GONE_EDGE_COUNT,
    _DEFAULT_GONE_SHARPNESS,
)
from menhir.services.lifecycle_service import LifecycleService
from menhir.services.saga_reconcile_dispatcher import SagaReconcileDispatcher, build_handlers
from menhir.services.saga_reconcile_outcomes import (
    WOULD_MARK_ALREADY_APPLIED,
    WOULD_NEEDS_REVIEW,
)
from tests.test_saga_reconcile_live_path import _Gate

pytestmark = pytest.mark.unit


@pytest.fixture()
def journal(tmp_path):
    j = GraphOperationsJournal(db_path=tmp_path / "ops.db")
    j._ensure_ready()
    return j


class _Adapter:
    """One live node; ``bridge_and_delete`` models the in-statement re-check refusing it."""

    def __init__(self, uuid, *, refuses=False, relationships=(), outcome=None, gate_passes=True):
        self.uuid = uuid
        self.present = True
        self.refuses = refuses
        self.relationships = list(relationships)
        self.outcome = outcome
        self.gate_passes = gate_passes
        self.calls = []

    def capture_node_state(self, uuid):
        return {"uuid": uuid, "labels": ["Entity"], "properties": {"name": "n"},
                "relationships": self.relationships} if (
            uuid == self.uuid and self.present) else None

    def newly_unreferenced_evidence(self, uuids):
        return []

    def decay_delete_gate_passes(self, node_uuid, **gate):
        return self.gate_passes

    def bridge_and_delete(self, node_uuid, **gate):
        self.calls.append((node_uuid, gate))
        if self.outcome is not None:
            return self.outcome
        if self.refuses:
            return {"edges_bridged": 0, "deleted": 0}
        self.present = False
        return {"edges_bridged": 3, "deleted": 1, "bridge_skipped_hubs": [node_uuid],
                "bridge_uuids": ["b1", "b2", "b3"]}


_GATE = {"min_days": 90.0, "max_edge_count": 3, "max_sharpness": 0.1}
_RECENCY = "2025-01-01T00:00:00Z"


def _candidate(**over):
    row = {
        "uuid": "n1", "type": "SEMANTIC", "scope": "PERSISTENT", "freshness": "COMPRESSED",
        "sharpness": 0.05, "edge_count": 0, "last_accessed_days_ago": 400.0,
        "recency": _RECENCY, "user_flagged": False,
    }
    row.update(over)
    return row


def _read(**over):
    read = {
        "read_type": "SEMANTIC", "read_edge_count": 0, "read_sharpness": 0.05,
        "read_recency": _RECENCY, "requires_target_date": False,
    }
    read.update(over)
    return read


def test_decay_delete_journals_a_snapshot_before_deleting(journal):
    adapter = _Adapter("n1")
    coord = DeleteCoordinator(graph_adapter=adapter, journal=journal)

    result = coord.delete_decayed_node("n1", candidate=_candidate(), **_GATE)

    assert result["deleted"] == ["n1"]
    assert (result["edges_bridged"], result["bridge_skipped_hubs"]) == (3, ["n1"])
    assert result["bridge_uuids"] == ["b1", "b2", "b3"]
    assert adapter.calls == [("n1", {
        **_GATE, **_read(), "read_rel_count": 0, "read_rel_signatures": []})], (
        "every fact the decision was read from is passed to the statement"
    )
    row = journal.get(result["op_id"])
    assert row["operation_kind"] == "DECAY_DELETE"
    assert row["state"] == "COMMITTED"
    assert json.loads(row["before_snapshot_json"])["nodes"]["n1"], "snapshot outlives the node"
    request = json.loads(row["request_json"])
    assert request["decay_gate"] == {**_GATE, **_read()}
    assert request["relationship_count"] == 0


def test_the_statement_gets_the_snapshots_relationship_fingerprint(journal):
    rels = [
        {"type": "RELATES_TO", "direction": "out", "properties": {}, "peer_uuid": "p1",
         "peer_labels": ["Entity"], "peer_identity": "p1"},
        {"type": "RELATES_TO", "direction": "out", "properties": {}, "peer_uuid": "p1",
         "peer_labels": ["Entity"], "peer_identity": "p1"},
        {"type": "MENTIONS", "direction": "in", "properties": {}, "peer_uuid": "e1",
         "peer_labels": ["Episodic"], "peer_identity": "e1"},
    ]
    adapter = _Adapter("n1", relationships=rels)
    coord = DeleteCoordinator(graph_adapter=adapter, journal=journal)

    coord.delete_decayed_node("n1", candidate=_candidate(), **_GATE)

    gate = adapter.calls[0][1]
    assert gate["read_rel_count"] == 3, "parallel edges are counted"
    assert gate["read_rel_signatures"] == ["MENTIONS|in|e1", "RELATES_TO|out|p1"]


def test_decay_delete_refused_by_the_statement_is_skipped_not_deleted(journal):
    adapter = _Adapter("n1", refuses=True)
    coord = DeleteCoordinator(graph_adapter=adapter, journal=journal)

    result = coord.delete_decayed_node("n1", candidate=_candidate(), **_GATE)

    assert result["deleted"] == []
    assert result["skipped"] == ["n1"]
    assert result["reason"] == GATE_REFUSED
    assert adapter.present
    # A refusal changed nothing, so the row closes COMMITTED without graph work: not open, not
    # quarantined, not FAILED.
    assert journal.get(result["op_id"])["state"] == "COMMITTED"


def test_a_hub_over_the_cap_is_refused_and_reported(journal):
    adapter = _Adapter("n1", outcome={
        "edges_bridged": 0, "deleted": 0, "bridge_skipped_hubs": ["n1"],
        "reason": "bridge_cap_exceeded"})
    coord = DeleteCoordinator(graph_adapter=adapter, journal=journal)

    result = coord.delete_decayed_node("n1", candidate=_candidate(), **_GATE)

    assert result["deleted"] == [] and result["skipped"] == ["n1"]
    assert result["reason"] == "bridge_cap_exceeded"
    assert result["bridge_skipped_hubs"] == ["n1"]
    assert adapter.present
    assert journal.get(result["op_id"])["state"] == "COMMITTED"


@pytest.mark.parametrize("over", [
    {"type": "IDENTITY"},                     # decay-exempt type
    {"type": "SPATIAL", "edge_count": 2},     # passes the global floor, not SPATIAL's own (< 2)
    {"type": "PREFERENCE", "last_accessed_days_ago": 100.0},  # idle 100 < 180 days
    {"type": "TEMPORAL"},                     # target date has not passed
])
def test_the_node_types_own_policy_declines_before_anything_is_journaled(journal, over):
    adapter = _Adapter("n1")
    coord = DeleteCoordinator(graph_adapter=adapter, journal=journal)

    result = coord.delete_decayed_node("n1", candidate=_candidate(**over), **_GATE)

    assert result == {"deleted": [], "skipped": ["n1"], "reason": POLICY_DECLINED}
    assert adapter.calls == []
    assert journal.list_by_state("PREPARED") == []


def test_a_temporal_node_past_its_target_date_binds_the_target_date_into_the_statement(journal):
    adapter = _Adapter("n1")
    coord = DeleteCoordinator(graph_adapter=adapter, journal=journal)

    coord.delete_decayed_node(
        "n1", candidate=_candidate(type="TEMPORAL", target_date_passed=True), **_GATE)

    gate = adapter.calls[0][1]
    assert gate["read_type"] == "TEMPORAL" and gate["requires_target_date"] is True


def test_missing_read_values_decline(journal):
    coord = DeleteCoordinator(graph_adapter=_Adapter("n1"), journal=journal)

    result = coord.delete_decayed_node("n1", candidate=_candidate(recency=None), **_GATE)

    assert result["reason"] == MISSING_READ_VALUES and result["deleted"] == []


def test_dry_run_reports_would_delete_only_when_the_gate_passes(journal):
    passing = DeleteCoordinator(graph_adapter=_Adapter("n1"), journal=journal)
    refusing = DeleteCoordinator(graph_adapter=_Adapter("n1", gate_passes=False), journal=journal)

    ok = passing.delete_decayed_node("n1", candidate=_candidate(), dry_run=True, **_GATE)
    no = refusing.delete_decayed_node("n1", candidate=_candidate(), dry_run=True, **_GATE)

    assert ok["would_delete"] == ["n1"]
    assert no["would_delete"] == [] and no["reason"] == GATE_REFUSED
    assert journal.list_by_state("PREPARED") == [], "a dry run journals nothing"


def test_prepared_decay_delete_is_classified_by_the_reconciler(journal):
    journal.prepare(
        operation_kind="DECAY_DELETE",
        request_json=json.dumps({"op_id": "d1", "targets": ["n1"]}),
        target_key="del-d1",
        op_id="d1",
    )
    row = journal.get("d1")

    gone = DeleteCoordinator(graph_adapter=_Adapter("other"), journal=journal)
    assert gone.classify_prepared_row(row) == (WOULD_MARK_ALREADY_APPLIED, {})

    survivor = DeleteCoordinator(graph_adapter=_Adapter("n1"), journal=journal)
    outcome, diag = survivor.classify_prepared_row(row)
    assert outcome == WOULD_NEEDS_REVIEW and diag["survivors"] == ["n1"]


def test_dispatcher_routes_decay_delete_to_the_delete_coordinator(journal):
    coord = DeleteCoordinator(graph_adapter=_Adapter("n1"), journal=journal)
    assert build_handlers(delete=coord)["DECAY_DELETE"] is coord


def _abandoned_decay_row(journal, op_id, targets):
    stamp = "2026-01-01T00:00:00+00:00"
    with sqlite3.connect(journal.db_path) as conn:
        conn.execute(
            "INSERT INTO graph_operations (op_id, operation_kind, request_json, state, "
            "created_at, updated_at, owner_token, owner_lease_expires_at) "
            "VALUES (?, 'DECAY_DELETE', ?, 'PREPARED', ?, ?, ?, ?)",
            (op_id, json.dumps({"op_id": op_id, "targets": targets}), stamp, stamp,
             f"inst:{process_liveness.hostname()}:999999:deadnonce", "2020-01-01T00:00:00+00:00"),
        )
        conn.commit()


@pytest.mark.usefixtures("pid_namespace_verifiable")
@pytest.mark.parametrize("node_survives,state", [(False, "COMMITTED"), (True, "NEEDS_REVIEW")])
def test_the_dispatcher_decides_a_crashed_decay_delete_row(journal, node_survives, state):
    _abandoned_decay_row(journal, "crashed", ["n1"])
    adapter = _Adapter("n1")
    adapter.present = node_survives
    coord = DeleteCoordinator(graph_adapter=adapter, journal=journal)

    SagaReconcileDispatcher(journal=journal, handlers=build_handlers(delete=coord)).run(
        dry_run=False, gate=_Gate())

    assert journal.get("crashed")["state"] == state


def _service(tmp_path, candidates):
    graph = MagicMock()
    graph.sync_edge_counts.return_value = 0
    graph.fetch_decay_candidates.side_effect = lambda freshness, **kw: (
        [dict(c) for c in candidates] if str(freshness).upper().endswith("COMPRESSED") else []
    )
    svc = LifecycleService(
        graph_adapter=graph, graphiti_client=MagicMock(), llm=None,
        pending_actions=PendingActionStore(db_path=tmp_path / "decay.db"),
        settings=MemorySettings(),
    )
    return graph, svc


@pytest.mark.asyncio
async def test_decay_sweep_deletes_only_through_the_coordinator(monkeypatch, tmp_path):
    monkeypatch.setenv("MENHIR_AUTO_DELETE_MODE", "on")
    graph, svc = _service(tmp_path, [_candidate(uuid="d1")])
    coordinator = MagicMock()
    coordinator.delete_decayed_node.return_value = {
        "deleted": ["d1"], "edges_bridged": 2, "bridge_skipped_hubs": [],
    }
    with patch.object(LifecycleService, "should_delete", staticmethod(lambda node: True)), \
            patch.object(LifecycleService, "_delete_coordinator", lambda self: coordinator):
        result = await svc.apply_decay()

    graph.bridge_and_delete.assert_not_called()
    coordinator.delete_decayed_node.assert_called_once()
    assert coordinator.delete_decayed_node.call_args.args == ("d1",)
    kwargs = coordinator.delete_decayed_node.call_args.kwargs
    assert {k: v for k, v in kwargs.items() if k != "candidate"} == {
        "min_days": _DEFAULT_GONE_DAYS,
        "max_edge_count": _DEFAULT_GONE_EDGE_COUNT,
        "max_sharpness": _DEFAULT_GONE_SHARPNESS,
    }
    assert (_DEFAULT_GONE_DAYS, _DEFAULT_GONE_EDGE_COUNT, _DEFAULT_GONE_SHARPNESS) == (30, 3, 0.1)
    assert kwargs["candidate"]["uuid"] == "d1" and kwargs["candidate"]["recency"] == _RECENCY
    assert (result.deleted, result.edges_bridged) == (1, 2)


@pytest.mark.asyncio
async def test_one_failing_delete_does_not_end_the_sweep_and_refusals_are_logged(
    monkeypatch, tmp_path, caplog
):
    monkeypatch.setenv("MENHIR_AUTO_DELETE_MODE", "on")
    _, svc = _service(tmp_path, [
        _candidate(uuid="d1"), _candidate(uuid="d2"), _candidate(uuid="d3")])
    coordinator = MagicMock()
    coordinator.delete_decayed_node.side_effect = [
        RuntimeError("deadlock after retries"),
        {"deleted": [], "skipped": ["d2"], "reason": "bridge_cap_exceeded",
         "bridge_skipped_hubs": ["d2"]},
        {"deleted": ["d3"], "edges_bridged": 1, "bridge_skipped_hubs": []},
    ]
    with caplog.at_level(logging.INFO), \
            patch.object(LifecycleService, "should_delete", staticmethod(lambda node: True)), \
            patch.object(LifecycleService, "_delete_coordinator", lambda self: coordinator):
        result = await svc.apply_decay()

    assert coordinator.delete_decayed_node.call_count == 3
    assert (result.deleted, result.edges_bridged) == (1, 1)
    assert "d2 declined: bridge_cap_exceeded" in caplog.text
    assert "failed=1" in caplog.text and "bridge_skipped_hubs=['d2']" in caplog.text


class _Neo4j:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def execute(self, query, params=None, **kwargs):
        self.calls.append((query, params))
        self.kwargs = kwargs
        return self.rows


def test_bridge_and_delete_has_no_ungated_form():
    from menhir.infrastructure.consolidation_queries import ConsolidationRepository

    repo = ConsolidationRepository(_Neo4j([]))

    with pytest.raises(TypeError):
        repo.bridge_and_delete("n1")
    with pytest.raises(TypeError):
        repo.bridge_and_delete("n1", min_days=90.0, max_edge_count=3, max_sharpness=0.1)


def test_bridge_estimate_sums_capped_pairs_and_names_the_hubs():
    from menhir.infrastructure.consolidation_queries import (
        MAX_BRIDGE_NEIGHBOURS,
        ConsolidationRepository,
    )

    repo = ConsolidationRepository(_Neo4j([
        {"uuid": "small", "k": 4},
        {"uuid": "edge", "k": MAX_BRIDGE_NEIGHBOURS},
        {"uuid": "hub", "k": MAX_BRIDGE_NEIGHBOURS + 1},
    ]))

    out = repo.estimate_bridges_for_nodes(["small", "edge", "hub"])

    assert out["over_cap"] == ["hub"]
    assert out["estimated_edges"] == 6 + MAX_BRIDGE_NEIGHBOURS * (MAX_BRIDGE_NEIGHBOURS - 1) // 2
    assert out["neighbour_counts"]["hub"] == MAX_BRIDGE_NEIGHBOURS + 1


def test_batch_bridge_reports_skipped_hubs_and_keeps_the_int_wrapper():
    from menhir.infrastructure.consolidation_queries import ConsolidationRepository

    repo = ConsolidationRepository(_Neo4j([{"total_edges_bridged": 5, "skipped_hubs": ["h1"]}]))

    assert repo.bridge_edges_for_nodes_detailed(["a", "h1"]) == {
        "edges_bridged": 5, "bridge_skipped_hubs": ["h1"]}
    assert repo.bridge_edges_for_nodes(["a", "h1"]) == 5


def test_duplicate_uuids_are_removed_before_bridging_and_estimating():
    from menhir.infrastructure.consolidation_queries import ConsolidationRepository

    bridging = _Neo4j([{"total_edges_bridged": 1, "skipped_hubs": []}])
    estimating = _Neo4j([])

    ConsolidationRepository(bridging).bridge_edges_for_nodes_detailed(["a", "a", "b"])
    ConsolidationRepository(estimating).estimate_bridges_for_nodes(["a", "b", "a"])

    assert bridging.calls[0][1]["node_uuids"] == ["a", "b"]
    assert estimating.calls[0][1]["node_uuids"] == ["a", "b"]


def test_the_decay_delete_statement_is_bounded_like_every_other_saga_mutation():
    from menhir.infrastructure.consolidation_queries import ConsolidationRepository
    from menhir.infrastructure.neo4j import SAGA_MUTATION_TIMEOUT_S

    neo4j = _Neo4j([])
    ConsolidationRepository(neo4j).bridge_and_delete(
        "n1", **_GATE, **_read(), read_rel_count=0, read_rel_signatures=[])

    assert neo4j.kwargs["timeout_s"] == SAGA_MUTATION_TIMEOUT_S
