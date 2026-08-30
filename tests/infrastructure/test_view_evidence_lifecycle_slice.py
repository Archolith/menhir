"""Focused unit coverage for activation-gated View evidence lifecycle infrastructure."""

from __future__ import annotations

from typing import Any

import pytest

from menhir.infrastructure.consolidation_queries import (
    ConsolidationRepository,
    automatic_lifecycle_protection_cypher,
)
from menhir.infrastructure.schema import (
    get_phase1_bootstrap_queries,
    get_view_evidence_lifecycle_activation_queries,
)


class _CaptureNeo4j:
    def __init__(self, responses: list[list[dict[str, Any]]] | None = None) -> None:
        self.responses = list(responses or [])
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def execute(
        self,
        query: str,
        params: dict[str, Any] | None = None,
        **_kwargs: Any,
    ) -> list[dict[str, Any]]:
        self.calls.append((query, params or {}))
        return self.responses.pop(0) if self.responses else []


def _assert_live_fact_retention_guard(query: str, variable: str = "n") -> None:
    assert f"MATCH ({variable})-[:MENTIONS]->(retaining_view:Entity)" in query
    assert "coalesce(retaining_view.is_view, false)" in query
    assert "coalesce(retaining_view.view_current, retaining_view.qs_current, true)" in query
    assert "NOT coalesce(retaining_view.retired, false)" in query


@pytest.mark.unit
def test_view_evidence_schema_is_optional_and_complete() -> None:
    activation = get_view_evidence_lifecycle_activation_queries()
    joined = "\n".join(activation)

    assert len(activation) == 8
    assert "EvidenceNamespaceFence) REQUIRE f.namespace_key IS UNIQUE" in joined
    assert "EvidencePublicationIntent) REQUIRE i.intent_key IS UNIQUE" in joined
    assert "EvidencePublicationIntent) ON (i.status)" in joined
    assert "EvidenceTombstone) REQUIRE t.tombstone_key IS UNIQUE" in joined
    assert "EvidenceTombstone) ON (t.digest)" in joined
    assert "EvidenceTombstone) ON (t.key_id)" in joined
    assert "ViewProjectionRepair) REQUIRE r.repair_key IS UNIQUE" in joined
    assert "ViewProjectionRepair) ON (r.status, r.lease_expires_at)" in joined

    phase_one = "\n".join(get_phase1_bootstrap_queries())
    for label in (
        "EvidenceNamespaceFence",
        "EvidencePublicationIntent",
        "EvidenceTombstone",
        "ViewProjectionRepair",
    ):
        assert label not in phase_one


@pytest.mark.unit
def test_automatic_lifecycle_guard_targets_only_current_nonretired_fact_views() -> None:
    predicate = automatic_lifecycle_protection_cypher("evidence")

    _assert_live_fact_retention_guard(predicate, "evidence")
    assert "NOT coalesce(evidence.is_view, false)" in predicate
    assert "NOT coalesce(evidence.is_quantstate, false)" in predicate
    assert "evidence.view_kind IS NULL" in predicate


@pytest.mark.unit
def test_decay_bridge_and_session_delete_paths_apply_live_fact_retention_guard() -> None:
    neo4j = _CaptureNeo4j()
    repo = ConsolidationRepository(neo4j)

    repo.fetch_decay_candidates(
        "ACTIVE",
        min_days_since_accessed=30,
        max_edge_count=2,
    )
    repo.bridge_and_delete("evidence-1")
    repo.delete_session_nodes(["evidence-1"])
    repo.delete_entities_returning_uuids(["evidence-1"], require_scope="SESSION")
    repo.fetch_ttl_expired_session_uuids("session-1")

    assert len(neo4j.calls) == 5
    for query, _params in neo4j.calls:
        _assert_live_fact_retention_guard(query)


@pytest.mark.unit
def test_other_automatic_lifecycle_mutations_apply_live_fact_retention_guard() -> None:
    neo4j = _CaptureNeo4j()
    repo = ConsolidationRepository(neo4j)

    repo.compress_node("evidence-1", "summary")
    repo.complete_rehydration("evidence-1", "full content")
    repo.update_sharpness("evidence-1", 0.2)
    repo.fetch_session_entities("session-1")
    repo.promote_to_persistent(["evidence-1"])
    repo.set_demote_ttl(["evidence-1"], 7)

    assert len(neo4j.calls) == 7
    for query, _params in neo4j.calls:
        _assert_live_fact_retention_guard(query)


@pytest.mark.unit
def test_conflict_candidate_and_status_mutations_apply_live_fact_retention_guard() -> None:
    neo4j = _CaptureNeo4j()
    repo = ConsolidationRepository(neo4j)

    repo.set_conflict("a", "b", "group-1")
    repo.set_conflict_group_status("group-1", "resolved")
    repo.requeue_conflicts_for_llm_review()
    repo.list_conflict_groups()

    set_conflict_query = neo4j.calls[0][0]
    for variable in ("a", "b", "orphan"):
        _assert_live_fact_retention_guard(set_conflict_query, variable)
    _assert_live_fact_retention_guard(neo4j.calls[1][0])
    _assert_live_fact_retention_guard(neo4j.calls[2][0])
    _assert_live_fact_retention_guard(neo4j.calls[2][0], "m")
    _assert_live_fact_retention_guard(neo4j.calls[3][0])


@pytest.mark.unit
def test_conflict_gone_and_bridge_mutations_apply_live_fact_retention_guard() -> None:
    neo4j = _CaptureNeo4j(
        responses=[
            [{"uuid": "keep-1"}, {"uuid": "remove-1"}],
            [
                {"uuid": "keep-1", "scope": "PERSISTENT", "name": "Keep"},
                {"uuid": "remove-1", "scope": "PERSISTENT", "name": "Remove"},
            ],
            [{"content": "keep", "original_content": None}],
            [{"content": ""}],
            [{"removed_uuids": ["remove-1"]}],
            [{"resolved": 1}],
            [{"total_edges_bridged": 0}],
        ]
    )
    repo = ConsolidationRepository(neo4j)

    result = repo.resolve_conflict_group(
        "group-1",
        "replace",
        keep_uuid="keep-1",
        remove_uuid="remove-1",
    )

    assert result["removed_uuids"] == ["remove-1"]
    gone_query = next(query for query, _ in neo4j.calls if "n.freshness = 'GONE'" in query)
    bridge_query = next(query for query, _ in neo4j.calls if "total_edges_bridged" in query)
    _assert_live_fact_retention_guard(gone_query)
    _assert_live_fact_retention_guard(bridge_query)
