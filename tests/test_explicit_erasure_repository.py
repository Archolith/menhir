"""Focused query-contract tests for fenced evidence production and erasure."""

from __future__ import annotations

from typing import Any

import pytest

from menhir.infrastructure.memory_queries import (
    MemoryQueryRepository,
    _validated_evidence_tombstone_params,
)
from menhir.infrastructure.turn_evidence_repository import TurnEvidenceRepository


class _Neo4j:
    def __init__(self, responses: list[list[dict[str, Any]]]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def execute(
        self, query: str, params: dict[str, Any] | None = None, **_kwargs: Any
    ) -> list[dict[str, Any]]:
        self.calls.append((query, params or {}))
        return self.responses.pop(0) if self.responses else []


@pytest.mark.unit
def test_turn_evidence_producer_locks_fence_before_merge_and_stamps_generation() -> None:
    neo4j = _Neo4j([[{
        "turn_id": "turn-1",
        "created": True,
        "recorded_at": "2026-08-28T00:00:00Z",
    }]])

    result = TurnEvidenceRepository(neo4j).record_turn_evidence(
        text="I own three lenses", namespace="project", prompt_id="prompt-1"
    )

    query, params = neo4j.calls[0]
    assert query.index("MERGE (f:EvidenceNamespaceFence") < query.index(
        "MERGE (t:TurnEvidence"
    )
    assert query.index("SET f.lock_nonce") < query.index("MERGE (t:TurnEvidence")
    assert "t.evidence_finalized = true" in query
    assert "t.evidence_generation = f.generation" in query
    assert params["namespace_key"] == "project"
    assert result["turn_id"] == "turn-1"


@pytest.mark.unit
def test_single_erasure_preflight_is_revalidated_only_after_namespace_lock() -> None:
    neo4j = _Neo4j([
        [{"namespace_keys": ["project"]}],
        [{
            "memory_touched": 1,
            "assertions_deleted": 0,
            "heads_deleted": 0,
            "dependent_views_retired": 1,
            "dependent_views_scrubbed": 1,
            "view_repairs_created": 1,
            "watermarks_reset": 1,
            "repairs": [],
        }],
    ])

    result = MemoryQueryRepository(neo4j).delete_memory_with_scalar_cascade(
        "evidence-1", operation_id="erase-1"
    )

    assert len(neo4j.calls) == 2
    mutation, params = neo4j.calls[1]
    fence_at = mutation.index("MERGE (f:EvidenceNamespaceFence")
    revalidation_at = mutation.index("OPTIONAL MATCH (target)")
    dependent_read_at = mutation.index("OPTIONAL MATCH (v:Entity)")
    assert fence_at < revalidation_at < dependent_read_at
    assert "all(actual_key IN actual_namespace_keys" in mutation
    assert params["namespace_key"] == "project"
    assert result["view_repairs_created"] == 1


@pytest.mark.unit
def test_single_erasure_deletes_a_direct_turn_evidence_target() -> None:
    neo4j = _Neo4j([
        [{"namespace_keys": ["project"]}],
        [{
            "memory_touched": 1,
            "assertions_deleted": 0,
            "heads_deleted": 0,
            "dependent_views_retired": 2,
            "dependent_views_scrubbed": 2,
            "view_repairs_created": 2,
            "watermarks_reset": 1,
            "repairs": [],
        }],
    ])

    result = MemoryQueryRepository(neo4j).delete_memory_with_scalar_cascade(
        "turn-1", operation_id="erase-turn-1"
    )

    mutation = neo4j.calls[1][0]
    assert "(n:TurnEvidence AND n.turn_id = $node_uuid)" in mutation
    assert "WITH n WHERE n:TurnEvidence" in mutation
    assert result["touched"] is True
    assert result["memory_touched"] == 1
    assert result["dependent_views_retired"] == 2


@pytest.mark.unit
def test_view_repairs_use_approved_status_and_deterministic_source_families() -> None:
    neo4j = _Neo4j([
        [{"namespace_keys": ["project"]}],
        [{"memory_touched": 1}],
    ])

    MemoryQueryRepository(neo4j).delete_memory_with_scalar_cascade(
        "evidence-1", operation_id="erase-1"
    )

    mutation = neo4j.calls[1][0]
    assert "'typed_scalar_assertions'" in mutation
    assert "'typed_event_assertions'" in mutation
    assert "[:EVENT_HISTORY_ENTRY]->(source:TypedEventAssertion)" in mutation
    assert "WHEN repair.reconstructible THEN 'pending'" in mutation
    assert "ELSE 'terminal_not_rebuildable'" in mutation
    assert "ELSE 'blocked'" not in mutation
    assert "rr.reconstructible = size(coalesce" not in mutation
    assert "rr.view_subtype = repair.view.view_subtype" in mutation
    assert "rr.subject_uuid = repair.view.view_subject_uuid" in mutation
    assert "rr.predicate = repair.view.view_predicate" in mutation
    assert "rr.domain = coalesce(repair.view.view_domain, '')" in mutation
    assert "rr.fence_generation = head(fences).generation" in mutation


@pytest.mark.unit
def test_direct_turn_purge_fences_generation_and_removes_all_related_state() -> None:
    neo4j = _Neo4j([[{"c": 2}]])

    assert TurnEvidenceRepository(neo4j).purge_namespace("project") == 2

    query, params = neo4j.calls[0]
    assert query.index("MERGE (f:EvidenceNamespaceFence") < query.index(
        "OPTIONAL MATCH (t:TurnEvidence)"
    )
    assert "f.generation = coalesce(f.generation, 0) + 1" in query
    assert "MATCH (source:TurnEvidence)-[:MENTIONS]->(v)" in query
    assert "v.turn_evidence_uuid IN doomed_ids" in query
    assert "OPTIONAL MATCH (a:TypedAssertion)" in query
    assert "OPTIONAL MATCH (event:TypedEventAssertion)" in query
    assert "stale_repair:ScalarProjectionRepair" in query
    assert "stale_repair:ViewProjectionRepair" in query
    assert "ELSE 'terminal_not_rebuildable'" in query
    assert "rr.subject_uuid = repair.view.view_subject_uuid" in query
    assert "rr.domain = coalesce(repair.view.view_domain, '')" in query
    assert "rr.fence_generation = f.generation" in query
    assert params["namespace_key"] == "project"


@pytest.mark.unit
def test_tombstone_boundary_requires_caller_supplied_opaque_digest_and_key() -> None:
    assert _validated_evidence_tombstone_params(
        evidence_digest=None, digest_key_id=None
    ) is None
    with pytest.raises(ValueError, match="both an opaque digest"):
        _validated_evidence_tombstone_params(
            evidence_digest="a" * 64, digest_key_id=None
        )
    with pytest.raises(ValueError, match="opaque base64url"):
        _validated_evidence_tombstone_params(
            evidence_digest="raw evidence id", digest_key_id="key-1"
        )
    assert _validated_evidence_tombstone_params(
        evidence_digest="a" * 64, digest_key_id="key-1"
    ) == {"evidence_digest": "a" * 64, "digest_key_id": "key-1"}
