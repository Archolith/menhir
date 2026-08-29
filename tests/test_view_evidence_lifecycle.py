"""Invariant tests for materialized View evidence retention and erasure."""

from __future__ import annotations

from typing import Any

import pytest

from menhir.domain.recall_visibility import (
    default_recall_visibility_cypher,
    view_live_provenance_cypher,
)
from menhir.infrastructure.memory_queries import MemoryQueryRepository
from menhir.infrastructure.consolidation_queries import ConsolidationRepository
from menhir.infrastructure.turn_evidence_repository import TurnEvidenceRepository
from menhir.infrastructure.view_repository import ViewRepository


class _CaptureNeo4j:
    def __init__(self, response: list[dict[str, Any]] | None = None) -> None:
        self.response = response or []
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def execute(self, query: str, params: dict[str, Any] | None = None, **_kwargs: Any):
        self.calls.append((query, params or {}))
        return self.response


@pytest.mark.unit
def test_default_recall_visibility_requires_current_live_view_provenance() -> None:
    predicate = default_recall_visibility_cypher("n")

    assert "scope, 'PERSISTENT') <> 'CANDIDATE'" in predicate
    assert "freshness, 'ACTIVE') <> 'GONE'" in predicate
    assert "n.view_class = 'FACT'" in predicate
    assert "n.view_audience = 'RECALL'" in predicate
    assert "coalesce(n.view_current, n.qs_current, false)" in predicate
    assert "NOT coalesce(n.retired, false)" in predicate
    assert view_live_provenance_cypher("n") in predicate
    assert "MATCH (e)-[:MENTIONS]->(n)" in predicate
    assert "COUNT { MATCH ()-[:MENTIONS]->(n) }" in predicate
    assert "coalesce(e.namespace, e.group_id, '')" in predicate
    assert "coalesce(n.namespace, n.group_id, '')" in predicate


@pytest.mark.unit
def test_fact_create_is_gated_before_supersession_when_evidence_is_missing() -> None:
    neo4j = _CaptureNeo4j()
    repo = ViewRepository(neo4j)

    with pytest.raises(ValueError, match="every declared contributor UUID"):
        repo.record(
            "counter",
            subject="user",
            namespace="project",
            counter="widgets",
            value=2,
            episode_uuids=["missing-evidence"],
        )

    create_query = next(query for query, _ in neo4j.calls if "CREATE (n:" in query)
    assert "OPTIONAL MATCH (e)" in create_query
    assert "(e:Episodic AND e.uuid = eid)" in create_query
    assert "(e:TurnEvidence AND e.turn_id = eid)" in create_query
    assert "$tenant_namespaces IS NULL OR" in create_query
    assert "collect(DISTINCT e)" in create_query
    assert "size(row.candidates) = 1" in create_query
    assert create_query.index("WHERE resolved_count = size($eps)") < create_query.index(
        "CREATE (n:"
    )
    assert "FOREACH (e IN evidence | MERGE (e)-[:MENTIONS]->(n))" in create_query


@pytest.mark.unit
def test_slot_keyed_view_injection_uses_the_same_live_evidence_gate() -> None:
    neo4j = _CaptureNeo4j()
    repo = ViewRepository(neo4j)

    assert repo.fetch_current_scalar_view_for_slot(
        subject_uuid="user-1",
        attribute="owned",
        scope="",
        value_kind="count",
        unit="",
        namespace="project",
    ) is None

    query = neo4j.calls[0][0]
    assert view_live_provenance_cypher("n") in query


@pytest.mark.unit
def test_explicit_evidence_erasure_retires_views_and_resets_rebuild_cursor() -> None:
    neo4j = _CaptureNeo4j([{
        "namespace_keys": ["project"],
        "memory_touched": 1,
        "assertions_deleted": 0,
        "heads_deleted": 0,
        "dependent_views_retired": 1,
        "dependent_views_scrubbed": 1,
        "watermarks_reset": 1,
        "repairs": [],
    }])
    repo = MemoryQueryRepository(neo4j)

    result = repo.delete_memory_with_scalar_cascade("evidence-1", operation_id="erase-1")

    query = next(
        query for query, _ in neo4j.calls
        if "v.retired_reason = 'contributing_evidence_erased'" in query
        and "MERGE (f:EvidenceNamespaceFence" in query
    )
    assert "$node_uuid IN coalesce(v.episode_uuids, [])" in query
    assert "v.retired_reason = 'contributing_evidence_erased'" in query
    assert "REMOVE v.ss_view_key_current" in query
    assert "w:ConsolidationWatermark OR w:ScalarConsolidationWatermark" in query
    assert "OR w:EventConsolidationWatermark" in query
    assert query.index("v.retired_reason") < query.index("DETACH DELETE n")
    assert result["dependent_views_retired"] == 1
    assert result["watermarks_reset"] == 1


@pytest.mark.unit
def test_direct_turn_evidence_purge_cannot_bypass_view_invalidation() -> None:
    neo4j = _CaptureNeo4j([{"c": 2}])
    repo = TurnEvidenceRepository(neo4j)

    assert repo.purge_namespace("project") == 2

    query = neo4j.calls[0][0]
    assert "t.turn_id" in query
    assert "v.retired_reason = 'contributing_evidence_erased'" in query
    assert "WHERE NOT eid IN doomed_ids" in query
    assert "w:ScalarConsolidationWatermark" in query
    assert query.index("v.retired_reason") < query.index("DETACH DELETE t")


@pytest.mark.unit
def test_normal_decay_never_selects_or_deletes_derived_views() -> None:
    neo4j = _CaptureNeo4j()
    repo = ConsolidationRepository(neo4j)

    repo.fetch_decay_candidates(
        "ACTIVE", min_days_since_accessed=30, max_edge_count=2
    )
    repo.bridge_and_delete("view-1")

    for query, _ in neo4j.calls:
        assert "NOT coalesce(n.is_view, false)" in query
        assert "NOT coalesce(n.is_quantstate, false)" in query
        assert "n.view_kind IS NULL" in query
