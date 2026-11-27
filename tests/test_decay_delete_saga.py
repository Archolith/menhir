"""DECAY_DELETE: the decay sweep's physical delete runs through the journaled DeleteCoordinator.

Offline. A real journal over a fake graph adapter; only the graph is faked. The Cypher predicates
and bridge rules are covered online in ``test_decay_delete_bridges_live.py``.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from menhir.config import MemorySettings
from menhir.infrastructure.graph_operations import GraphOperationsJournal
from menhir.infrastructure.pending_actions import PendingActionStore
from menhir.services.delete_coordinator import DeleteCoordinator
from menhir.services.lifecycle_service import LifecycleService
from menhir.services.saga_reconcile_dispatcher_routing import build_handlers
from menhir.services.saga_reconcile_outcomes import (
    WOULD_MARK_ALREADY_APPLIED,
    WOULD_NEEDS_REVIEW,
)

pytestmark = pytest.mark.unit


@pytest.fixture()
def journal(tmp_path):
    j = GraphOperationsJournal(db_path=tmp_path / "ops.db")
    j._ensure_ready()
    return j


class _Adapter:
    """One live node; ``bridge_and_delete`` models the in-statement re-check refusing it."""

    def __init__(self, uuid, *, refuses=False):
        self.uuid = uuid
        self.present = True
        self.refuses = refuses
        self.calls = []

    def capture_node_state(self, uuid):
        return {"uuid": uuid, "labels": ["Entity"], "properties": {"name": "n"},
                "relationships": []} if (uuid == self.uuid and self.present) else None

    def newly_unreferenced_evidence(self, uuids):
        return []

    def bridge_and_delete(self, node_uuid, **gate):
        self.calls.append((node_uuid, gate))
        if self.refuses:
            return {"edges_bridged": 0, "deleted": 0}
        self.present = False
        return {"edges_bridged": 3, "deleted": 1, "bridge_skipped_hubs": [node_uuid]}


_GATE = {"min_days": 90.0, "max_edge_count": 3, "max_sharpness": 0.1}


def test_decay_delete_journals_a_snapshot_before_deleting(journal):
    adapter = _Adapter("n1")
    coord = DeleteCoordinator(graph_adapter=adapter, journal=journal)

    result = coord.delete_decayed_node("n1", **_GATE)

    assert result["deleted"] == ["n1"]
    assert (result["edges_bridged"], result["bridge_skipped_hubs"]) == (3, ["n1"])
    assert adapter.calls == [("n1", _GATE)], "every delete predicate bound is passed to the statement"
    row = journal.get(result["op_id"])
    assert row["operation_kind"] == "DECAY_DELETE"
    assert row["state"] == "COMMITTED"
    assert json.loads(row["before_snapshot_json"])["nodes"]["n1"], "snapshot outlives the node"
    assert json.loads(row["request_json"])["decay_gate"] == _GATE


def test_decay_delete_refused_by_the_statement_is_skipped_not_deleted(journal):
    adapter = _Adapter("n1", refuses=True)
    coord = DeleteCoordinator(graph_adapter=adapter, journal=journal)

    result = coord.delete_decayed_node("n1", **_GATE)

    assert result["deleted"] == []
    assert result["skipped"] == ["n1"]
    assert adapter.present
    assert journal.get(result["op_id"])["state"] != "PREPARED", "a refused delete leaves no open row"


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


@pytest.mark.asyncio
async def test_decay_sweep_deletes_only_through_the_coordinator(monkeypatch, tmp_path):
    monkeypatch.setenv("MENHIR_AUTO_DELETE_MODE", "on")
    graph = MagicMock()
    graph.sync_edge_counts.return_value = 0
    graph.fetch_decay_candidates.side_effect = lambda freshness, **kw: (
        [{"uuid": "d1", "freshness": "COMPRESSED"}] if str(freshness).upper().endswith("COMPRESSED")
        else []
    )
    svc = LifecycleService(
        graph_adapter=graph, graphiti_client=MagicMock(), llm=None,
        pending_actions=PendingActionStore(db_path=tmp_path / "decay.db"),
        settings=MemorySettings(),
    )
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
    assert set(coordinator.delete_decayed_node.call_args.kwargs) == {
        "min_days", "max_edge_count", "max_sharpness"}
    assert (result.deleted, result.edges_bridged) == (1, 2)


class _Neo4j:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, query, params=None, **_kwargs):
        return self.rows


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
