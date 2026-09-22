from __future__ import annotations

from typing import Any

import pytest

from menhir.domain import legacy_snapshot as ls
from menhir.domain import merge_eligibility as me
from menhir.domain.retention import (
    destructive_retention_allowed_cypher,
    same_tenant_cypher,
    source_retention_protected_cypher,
)
from menhir.infrastructure.consolidation_queries import ConsolidationRepository
from menhir.infrastructure.correlation_queries import CorrelationRepository
from menhir.infrastructure.episode_stamping import EpisodeStampingRepository
from menhir.services.unmerge_coordinator import UnmergeCoordinator


pytestmark = pytest.mark.unit


class CaptureNeo4j:
    def __init__(self, responses: list[list[dict[str, Any]]] | None = None) -> None:
        self.responses = list(responses or [])
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def execute(
        self, query: str, params: dict[str, Any] | None = None, **_kwargs: Any
    ) -> list[dict[str, Any]]:
        self.calls.append((query, params or {}))
        return self.responses.pop(0) if self.responses else []


def test_retention_predicate_is_live_and_tenant_consistent() -> None:
    predicate = source_retention_protected_cypher("entity")
    assert (
        "MATCH (retention_source:Episodic)-[:RETENTION_SOURCE]->(entity)" in predicate
    )
    assert "coalesce(retention_source.user_flagged, false)" in predicate
    assert same_tenant_cypher("retention_source", "entity") in predicate
    assert (
        "coalesce(entity.user_flagged, false) = false"
        in destructive_retention_allowed_cypher("entity")
    )


def test_record_retention_sources_is_scoped_idempotent_and_skips_structure() -> None:
    neo4j = CaptureNeo4j([[{"linked": 2}]])
    repo = EpisodeStampingRepository()
    repo.neo4j = neo4j

    linked = repo.record_retention_sources(
        source_episode_uuid="episode-1",
        entity_uuids=["entity-1", "entity-1", "", "entity-2"],
        namespace="project-a",
    )

    assert linked == 2
    query, params = neo4j.calls[0]
    assert "MERGE (source)-[retention:RETENTION_SOURCE]->(entity)" in query
    assert "entity.structure_role IS NULL" in query
    assert "$tenant_namespaces" in query
    assert params["entity_uuids"] == ["entity-1", "entity-2"]
    assert params["tenant_namespaces"] == ["project-a"]


def test_record_retention_sources_empty_input_is_noop() -> None:
    neo4j = CaptureNeo4j()
    repo = EpisodeStampingRepository()
    repo.neo4j = neo4j
    assert (
        repo.record_retention_sources(
            source_episode_uuid="episode-1", entity_uuids=[], namespace="default"
        )
        == 0
    )
    assert neo4j.calls == []


def _signals(uuid: str, *, source_protected: bool = False) -> me.NodeSignals:
    return me.NodeSignals(
        uuid=uuid,
        exists=True,
        ineligible_role=False,
        namespace="default",
        freshness="ACTIVE",
        scope="PERSISTENT",
        user_flagged=False,
        conflict_status=None,
        source_retention_protected=source_protected,
    )


def test_source_retention_vetoes_merge_preflight_and_final_mutation() -> None:
    result = me.evaluate(
        _signals("survivor"), _signals("absorbed", source_protected=True)
    )
    assert result.allowed is False
    assert result.reason_code == me.SOURCE_RETENTION_PROTECTED
    predicate = me.mutable_eligibility_cypher()
    assert predicate.count("RETENTION_SOURCE") == 2


def test_only_harmful_lifecycle_paths_inherit_source_retention() -> None:
    neo4j = CaptureNeo4j(
        [
            [{"updated": 1}],
            [{"updated": 1}],
            [{"promoted": 1}],
            [{"newly_demoted_count": 1}],
        ]
    )
    repo = ConsolidationRepository(neo4j)

    assert repo.compress_node("entity-1", "summary") is True
    assert repo.complete_rehydration("entity-1", "content") is True
    assert repo.promote_to_persistent(["entity-1"]) == 1
    assert repo.set_demote_ttl(["entity-1"], 7) == 1

    compress_query, rehydrate_query, promote_query, ttl_query = [
        q for q, _ in neo4j.calls
    ]
    assert "RETENTION_SOURCE" in compress_query
    assert "RETENTION_SOURCE" in ttl_query
    assert "RETENTION_SOURCE" not in rehydrate_query
    assert "RETENTION_SOURCE" not in promote_query
    assert len(neo4j.calls) == 4


def test_explicit_delete_does_not_inherit_automatic_retention_guard() -> None:
    neo4j = CaptureNeo4j([[{"deleted_uuids": []}], [{"deleted_uuids": []}]])
    repo = ConsolidationRepository(neo4j)

    repo.delete_entities_returning_uuids(["entity-1"])
    repo.delete_entities_returning_uuids(
        ["entity-1"], require_scope="SESSION", protect_retention=True
    )

    explicit_query, automatic_query = [query for query, _ in neo4j.calls]
    assert "RETENTION_SOURCE" not in explicit_query
    assert "RETENTION_SOURCE" in automatic_query


def test_unmerge_plan_removes_only_retention_support_added_by_merge() -> None:
    coordinator = object.__new__(UnmergeCoordinator)
    absorbed = {
        "labels": ["Entity"],
        "properties": {"uuid": "absorbed"},
        "relationships": [
            {
                "type": "RETENTION_SOURCE",
                "direction": "in",
                "peer_uuid": "shared",
                "properties": {},
            },
            {
                "type": "RETENTION_SOURCE",
                "direction": "in",
                "peer_uuid": "absorbed-only",
                "properties": {},
            },
        ],
    }
    survivor = {
        "properties": {"uuid": "survivor"},
        "relationships": [
            {
                "type": "RETENTION_SOURCE",
                "direction": "in",
                "peer_uuid": "shared",
                "properties": {},
            },
        ],
    }

    plan = coordinator._build_restore_plan(survivor, absorbed)
    assert plan["rebound_retention_sources"] == ["absorbed-only"]


def test_legacy_retention_source_round_trip_metadata() -> None:
    entry = {
        "retention_sources": ["shared", "absorbed-only"],
        "survivor_retention_sources_before": ["shared"],
    }
    assert ls.absorbed_retention_sources(entry) == ["shared", "absorbed-only"]
    assert ls.rebound_retention_sources(entry) == ["absorbed-only"]


def test_merge_queries_expand_tenant_guards(monkeypatch: pytest.MonkeyPatch) -> None:
    eligibility_rows = [
        {
            "uuid": uuid,
            "ineligible_role": False,
            "namespace": "default",
            "freshness": "ACTIVE",
            "scope": "PERSISTENT",
            "user_flagged": False,
            "source_retention_protected": False,
            "conflict_status": None,
        }
        for uuid in ("survivor", "absorbed")
    ]
    snapshot_row = {
        "uuid": "absorbed",
        "name": "absorbed",
        "source": "unit-test",
        "source_confidence": 0.5,
        "relationships": [],
        "mentioned_by_episodes": [],
        "survivor_episodes_before": [],
        "retention_sources": [],
        "survivor_retention_sources_before": [],
        "survivor_source": "unit-test",
        "survivor_source_confidence": 0.5,
    }
    neo4j = CaptureNeo4j(
        [
            eligibility_rows,
            [snapshot_row],
            [{"deleted": 1, "merge_namespace": "default"}],
        ]
    )
    monkeypatch.setattr(
        "menhir.infrastructure.telemetry.record_merge", lambda **_: None
    )

    result = CorrelationRepository(neo4j).merge_entity(
        "survivor", "absorbed", similarity=0.99
    )

    assert result["merged"] == 1
    snapshot_query = neo4j.calls[1][0]
    mutation_query = neo4j.calls[2][0]
    assert "__ABSORBED_RETENTION_TENANT__" not in snapshot_query + mutation_query
    assert "__SURVIVOR_RETENTION_TENANT__" not in snapshot_query
    assert "retention_source.namespace" in snapshot_query
    assert "retention_source.namespace" in mutation_query


def test_restore_query_expands_retention_tenant_guard() -> None:
    neo4j = CaptureNeo4j([[{"out_restored": 0}]])
    repo = CorrelationRepository(neo4j)

    result = repo.restore_merge_snapshot(
        survivor_uuid="survivor",
        absorbed_uuid="absorbed",
        absorbed_labels=["Entity"],
        absorbed_properties={"uuid": "absorbed"},
        out_rels=[],
        in_rels=[],
        survivor_properties={},
        rebound_episodes=[],
        rebound_retention_sources=["source"],
        operation_id="op-1",
    )

    assert result["restored"] == 1
    query = neo4j.calls[0][0]
    assert "__RETENTION_SOURCE_TENANT__" not in query
    assert "source.namespace" in query
