"""Survivor promotion, unmerge lineage and correlation-only edges (offline).

- a promotion candidate absorbed by a merge is promoted through its survivor. Before,
  promoting the absorbed uuid matched nothing and the SESSION survivor stayed sweepable.
- the unmerge veto follows the survivor's merged_from lineage, so a split pair cannot
  rejoin through the node that later absorbed the original survivor.
- create_related_to_edge counts and updates only correlation edges, never a Graphiti
  fact edge (also RELATES_TO), and locks the pair before its check-then-create.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from menhir.domain import merge_eligibility as me
from menhir.infrastructure.correlation_queries import CorrelationRepository
from menhir.services import lifecycle_consolidation
from menhir.services.lifecycle_service import LifecycleService

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- survivor promotion

class _MergeIntoSurvivor:
    """classify_pair stand-in: the candidate merges into ``survivor`` (a coordinator-style result)."""

    def __init__(self, survivor: str, *, report_survivor: bool = True) -> None:
        self.survivor = survivor
        self.report_survivor = report_survivor

    async def classify_pair(self, source: str, target: str, score: float):
        if target != self.survivor:
            return "none", None
        details = {"merged": 1, "op_id": "op-1"}
        if self.report_survivor:
            details.update(survivor_uuid=target, absorbed_uuid=source)
        return "merged", details


def _consolidation(adapter, graphiti, monkeypatch, correlation):
    adapter.session_entities = [
        {"uuid": "cand", "name": "candidate", "content": "candidate content", "user_flagged": False,
         "session_id": "s1", "namespace": "default"},
    ]
    adapter.persistent_edge_counts = {"cand": 5}  # a promotion candidate by edge count
    graphiti.search_scored_results = [("surv", "survivor", 0.97)]
    promoted_calls: list[list[str]] = []

    def _promote(uuids):
        promoted_calls.append(list(uuids))
        return [u for u in uuids if u != "cand"]  # the absorbed candidate no longer exists

    monkeypatch.setattr(adapter, "promote_to_persistent", _promote)
    revisions: list[str] = []
    monkeypatch.setattr(
        lifecycle_consolidation, "record_memory_revision",
        lambda **kw: revisions.append(kw["node_uuid"]),
    )
    svc = LifecycleService(graph_adapter=adapter, graphiti_client=graphiti)
    monkeypatch.setattr(svc, "_build_correlation_service", lambda: correlation)
    return svc, promoted_calls, revisions


@pytest.mark.asyncio
async def test_an_absorbed_promotion_candidate_promotes_its_survivor(
    stub_memory_graph_adapter, stub_graphiti_client, monkeypatch,
) -> None:
    svc, promoted_calls, revisions = _consolidation(
        stub_memory_graph_adapter, stub_graphiti_client, monkeypatch, _MergeIntoSurvivor("surv"),
    )

    result = await svc.consolidate_session("s1")

    assert promoted_calls == [["cand", "surv"]]
    assert revisions == ["surv"], "the survivor's promotion is audited like any other"
    assert result.promoted == 1


@pytest.mark.asyncio
async def test_no_survivor_is_promoted_without_one_in_the_merge_result(
    stub_memory_graph_adapter, stub_graphiti_client, monkeypatch,
) -> None:
    """The survivor comes from the merge result, never from a guess at the search hit."""
    svc, promoted_calls, revisions = _consolidation(
        stub_memory_graph_adapter, stub_graphiti_client, monkeypatch,
        _MergeIntoSurvivor("surv", report_survivor=False),
    )

    await svc.consolidate_session("s1")

    assert promoted_calls == [["cand"]] and revisions == []


@pytest.mark.asyncio
async def test_other_batch_callers_are_unchanged() -> None:
    """The conflict scan and candidate review call the batch without absorbed_into."""
    class _Host(lifecycle_consolidation.LifecycleConsolidationMixin):
        graph_adapter = MagicMock()
        graphiti_client = MagicMock()
        llm = None
        settings = SimpleNamespace(conflict_cooldown_days=30)

    host = _Host()
    host.graphiti_client.search_scored = AsyncMock(return_value=[("surv", "S", 0.97)])
    host._build_correlation_service = lambda: _MergeIntoSurvivor("surv")  # type: ignore[method-assign]
    assert await host._check_contradictions_batch([{"uuid": "cand", "name": "c"}]) == 0


# --------------------------------------------------------------------------- unmerge lineage

def _node(uuid, *, restored_from_merge=None, merged_from=()):
    return me.NodeSignals(
        uuid=uuid, exists=True, ineligible_role=False, namespace="default", freshness="ACTIVE",
        scope="SESSION", user_flagged=False, conflict_status=None,
        restored_from_merge=restored_from_merge, merged_from=tuple(merged_from),
    )


@pytest.mark.parametrize(
    "survivor, absorbed",
    [
        (_node("C", merged_from=("S",)), _node("B", restored_from_merge="S")),
        (_node("B", restored_from_merge="S"), _node("C", merged_from=("X", "S"))),
    ],
    ids=["absorbed-split-from-survivor-lineage", "survivor-split-from-absorbed-lineage"],
)
def test_the_veto_follows_merged_from_lineage(survivor, absorbed) -> None:
    assert me.evaluate(survivor, absorbed).reason_code == me.PREVIOUSLY_UNMERGED


def test_unrelated_lineage_does_not_veto() -> None:
    decision = me.evaluate(_node("C", merged_from=("X",)), _node("B", restored_from_merge="S"))
    assert decision.reason_code == me.ELIGIBLE


def test_the_repository_reads_merged_from() -> None:
    neo4j = MagicMock()
    base = {"ineligible_role": False, "namespace": "default", "freshness": "ACTIVE",
            "scope": "SESSION", "user_flagged": False, "memory_retired": False,
            "source_retention_protected": False, "conflict_status": None}
    neo4j.execute.return_value = [
        {"uuid": "C", **base, "restored_from_merge": None, "merged_from": ["S"]},
        {"uuid": "B", **base, "restored_from_merge": "S", "merged_from": []},
    ]
    assert CorrelationRepository(neo4j).evaluate_merge_eligibility("C", "B").reason_code == (
        me.PREVIOUSLY_UNMERGED
    )
    assert "coalesce(n.merged_from, []) AS merged_from" in neo4j.execute.call_args.args[0]


# --------------------------------------------------------------------------- correlation-only edges

def test_only_correlation_edges_are_matched_updated_or_merged() -> None:
    neo4j = MagicMock()
    neo4j.execute.return_value = []
    CorrelationRepository(neo4j).create_related_to_edge("u-1", "u-2", similarity=0.75)
    cypher = neo4j.execute.call_args.args[0]

    assert "[(a)-[e:RELATES_TO]-(b) WHERE e.type = 'correlation' | e]" in cypher
    assert "MERGE (a)-[r:RELATES_TO {type: 'correlation'}]->(b)" in cypher
    # Both nodes are write-locked before the existence check, so check-then-create is atomic.
    lock = cypher.index("SET a._correlation_lock = true, b._correlation_lock = true")
    assert lock < cypher.index("AS existing")
