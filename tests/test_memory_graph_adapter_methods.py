"""Unit tests for targeted graph adapter methods."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter, PolicyStampResult


@dataclass
class _StubNeo4jRepository:
    responses: list[list[dict[str, object]]] = field(default_factory=list)
    calls: list[dict[str, object]] = field(default_factory=list)

    def execute(self, query: str, params: dict[str, object] | None = None) -> list[dict[str, object]]:
        self.calls.append({"query": query, "params": params or {}})
        if self.responses:
            return self.responses.pop(0)
        return []


@pytest.mark.unit
def test_stamp_ingest_metadata_deduplicates_uuids_and_returns_counts() -> None:
    neo4j = _StubNeo4jRepository(
        responses=[
            [{"nodes_touched": 1}],
            [{"nodes_touched": 1}],
            [{"edges_touched": 1}],
        ]
    )
    adapter = MemoryGraphAdapter(neo4j=neo4j)

    result = adapter.stamp_ingest_metadata(
        node_uuids=["episode-1", "entity-1", "entity-1"],
        edge_uuids=["rel-1", "rel-1"],
        session_id="session-1",
        user_id="user-1",
        source="unit-test",
        source_confidence=0.5,
    )

    assert result == PolicyStampResult(nodes_touched=2, edges_touched=1)
    assert len(neo4j.calls) == 3
    assert neo4j.calls[0]["params"]["uuids"] == ["episode-1", "entity-1"]
    assert "MATCH (n:Episodic)" in neo4j.calls[0]["query"]
    assert neo4j.calls[1]["params"]["uuids"] == ["episode-1", "entity-1"]
    assert "MATCH (n:Entity)" in neo4j.calls[1]["query"]
    assert "AS locked" in neo4j.calls[1]["query"]
    assert "CASE WHEN locked THEN n.session_id" in neo4j.calls[1]["query"]
    assert neo4j.calls[2]["params"]["uuids"] == ["rel-1"]


@pytest.mark.unit
def test_sync_edge_counts_recounts_all_entity_relationships() -> None:
    neo4j = _StubNeo4jRepository(responses=[[{"synced": 4}]])
    adapter = MemoryGraphAdapter(neo4j=neo4j)

    result = adapter.sync_edge_counts()

    assert result == 4
    assert neo4j.calls[0]["params"] == {}
    assert "MATCH (n:Entity)" in neo4j.calls[0]["query"]
    assert "OPTIONAL MATCH (n)-[r]-()" in neo4j.calls[0]["query"]
    assert "SET n.edge_count = edge_count" in neo4j.calls[0]["query"]


@pytest.mark.unit
def test_stamp_ingest_metadata_skips_empty_batches() -> None:
    neo4j = _StubNeo4jRepository()
    adapter = MemoryGraphAdapter(neo4j=neo4j)

    result = adapter.stamp_ingest_metadata(
        node_uuids=[],
        edge_uuids=[],
        session_id="session-1",
        user_id="user-1",
        source="unit-test",
        source_confidence=0.5,
    )

    assert result == PolicyStampResult(nodes_touched=0, edges_touched=0)
    assert neo4j.calls == []


@pytest.mark.unit
def test_count_namespace_includes_namespace_keyed_episode_lifecycle_rows() -> None:
    neo4j = _StubNeo4jRepository(responses=[[{"total": 3}]])
    adapter = MemoryGraphAdapter(neo4j=neo4j)

    assert adapter.count_namespace("eval-ns", namespace="eval-ns") == 3
    query = neo4j.calls[0]["query"]
    assert "(n:Episodic AND n.namespace = $namespace)" in query
    assert neo4j.calls[0]["params"] == {
        "group_id": "eval-ns",
        "namespace": "eval-ns",
    }


@pytest.mark.unit
def test_count_namespace_counts_durable_event_log_in_namespace() -> None:
    # The namespace blast radius must include the durable event log: every :TypedEventAssertion in
    # the namespace and every :TypedEventAssertionHead that HAS_VERSION to an event assertion in the
    # namespace. :EventConsolidationWatermark is already caught by group_id (namespace == group_id).
    neo4j = _StubNeo4jRepository(responses=[[{"total": 5}]])
    adapter = MemoryGraphAdapter(neo4j=neo4j)

    assert adapter.count_namespace("eval-ns", namespace="eval-ns") == 5
    query = neo4j.calls[0]["query"]
    assert "(n:TypedEventAssertion AND n.namespace = $namespace)" in query
    assert "(n:TypedEventAssertionHead AND EXISTS {" in query
    assert "MATCH (n)-[:HAS_VERSION]->(ev:TypedEventAssertion) WHERE ev.namespace = $namespace" in query


@pytest.mark.unit
def test_flag_memory_returns_true_when_a_node_is_updated() -> None:
    # Step 4 added a structural-guard query before the flag query.
    # First response = empty (not a structural node), second = flag result.
    neo4j = _StubNeo4jRepository(responses=[[], [{"nodes_updated": 1}]])
    adapter = MemoryGraphAdapter(neo4j=neo4j)

    result = adapter.flag_memory("entity-123")

    assert result is True
    # First call is the structural guard; second is the flag SET.
    assert neo4j.calls[0]["params"] == {"node_uuid": "entity-123"}
    assert "structure_role" in neo4j.calls[0]["query"]
    assert "'directory:'" in neo4j.calls[0]["query"]
    assert neo4j.calls[1]["params"] == {"node_uuid": "entity-123"}
    assert "SET n.user_flagged = true" in neo4j.calls[1]["query"]
    assert "n.bootstrap_scope = n.bootstrap_scope" in neo4j.calls[1]["query"]


@pytest.mark.unit
def test_flag_memory_rejects_unmarked_legacy_structural_node() -> None:
    neo4j = _StubNeo4jRepository(
        responses=[[{"role": "legacy_project_scan"}]]
    )
    adapter = MemoryGraphAdapter(neo4j=neo4j)

    with pytest.raises(ValueError, match="role=legacy_project_scan"):
        adapter.flag_memory("legacy-directory")

    assert len(neo4j.calls) == 1
    assert "'directory:'" in neo4j.calls[0]["query"]


@pytest.mark.unit
def test_unflag_memory_returns_true_when_a_node_is_updated() -> None:
    neo4j = _StubNeo4jRepository(responses=[[{"nodes_updated": 1}]])
    adapter = MemoryGraphAdapter(neo4j=neo4j)

    result = adapter.unflag_memory("entity-123")

    assert result is True
    assert neo4j.calls[0]["params"] == {"node_uuid": "entity-123"}
    assert "SET n.user_flagged = false" in neo4j.calls[0]["query"]
    assert "n.bootstrap_scope = null" in neo4j.calls[0]["query"]


@pytest.mark.unit
def test_flag_memory_sets_explicit_workspace_bootstrap_scope() -> None:
    neo4j = _StubNeo4jRepository(responses=[[], [{"nodes_updated": 1}]])
    adapter = MemoryGraphAdapter(neo4j=neo4j)

    assert adapter.flag_memory(
        "entity-123", bootstrap_scope="workspace:Archolith"
    )
    assert neo4j.calls[1]["params"] == {
        "node_uuid": "entity-123",
        "bootstrap_scope": "workspace:archolith",
    }
    assert "n.bootstrap_scope = $bootstrap_scope" in neo4j.calls[1]["query"]


@pytest.mark.unit
def test_unflag_memory_returns_false_when_no_node_matched() -> None:
    neo4j = _StubNeo4jRepository(responses=[[{"nodes_updated": 0}]])
    adapter = MemoryGraphAdapter(neo4j=neo4j)

    result = adapter.unflag_memory("nonexistent-uuid")

    assert result is False
    assert neo4j.calls[0]["params"] == {"node_uuid": "nonexistent-uuid"}


@pytest.mark.unit
def test_unflag_memory_returns_false_on_empty_response() -> None:
    neo4j = _StubNeo4jRepository(responses=[[]])
    adapter = MemoryGraphAdapter(neo4j=neo4j)

    result = adapter.unflag_memory("empty-uuid")

    assert result is False


@pytest.mark.unit
def test_fetch_candidate_metadata_returns_scoring_fields() -> None:
    neo4j = _StubNeo4jRepository(
        responses=[[
            {
                "uuid": "e-1", "name": "Alice", "scope": "PERSISTENT",
                "type": "SEMANTIC", "content": "stuff", "summary": "sum",
                "last_accessed": None, "edge_count": 3,
                "sharpness": 0.5, "freshness": "ACTIVE", "user_flagged": False,
            },
        ]]
    )
    adapter = MemoryGraphAdapter(neo4j=neo4j)

    result = adapter.fetch_candidate_metadata(["e-1"])

    assert len(result) == 1
    assert result[0]["uuid"] == "e-1"
    assert result[0]["scope"] == "PERSISTENT"
    assert "MATCH (n:Entity)" in neo4j.calls[0]["query"]
    assert neo4j.calls[0]["params"]["uuids"] == ["e-1"]
    assert "n[$belief_commit_key] AS belief_commit" in neo4j.calls[0]["query"]
    assert neo4j.calls[0]["params"]["belief_commit_key"] == "belief_commit"


@pytest.mark.unit
def test_fetch_decay_candidates_returns_decay_fields() -> None:
    neo4j = _StubNeo4jRepository(
        responses=[[
            {
                "uuid": "e-1",
                "name": "Alpha",
                "content": "content",
                "summary": None,
                "scope": "PERSISTENT",
                "sharpness": 0.2,
                "edge_count": 1,
                "freshness": "ACTIVE",
                "user_flagged": False,
                "last_accessed": None,
                "created_at": None,
            }
        ]]
    )
    adapter = MemoryGraphAdapter(neo4j=neo4j)

    result = adapter.fetch_decay_candidates(
        "ACTIVE",
        min_days_since_accessed=30,
        max_edge_count=5,
    )

    assert len(result) == 1
    assert result[0]["uuid"] == "e-1"
    assert neo4j.calls[0]["params"]["freshness"] == "ACTIVE"
    assert neo4j.calls[0]["params"]["min_days_since_accessed"] == 30
    assert neo4j.calls[0]["params"]["max_edge_count"] == 5
    assert "n.scope = 'PERSISTENT'" in neo4j.calls[0]["query"]


@pytest.mark.unit
def test_fetch_candidate_metadata_empty_returns_empty() -> None:
    neo4j = _StubNeo4jRepository()
    adapter = MemoryGraphAdapter(neo4j=neo4j)

    result = adapter.fetch_candidate_metadata([])

    assert result == []
    assert neo4j.calls == []


@pytest.mark.unit
def test_fetch_adjacency_pairs_returns_connected_pairs() -> None:
    neo4j = _StubNeo4jRepository(
        responses=[[
            {"source": "a", "target": "b", "weight": 1.5, "edge_uuid": "r-1"},
        ]]
    )
    adapter = MemoryGraphAdapter(neo4j=neo4j)

    result = adapter.fetch_adjacency_pairs(["a", "b"])

    assert len(result) == 1
    assert result[0]["source"] == "a"
    assert result[0]["weight"] == 1.5
    assert "MATCH (a)-[r]-(b)" in neo4j.calls[0]["query"]


@pytest.mark.unit
def test_fetch_adjacency_pairs_single_uuid_returns_empty() -> None:
    neo4j = _StubNeo4jRepository()
    adapter = MemoryGraphAdapter(neo4j=neo4j)

    result = adapter.fetch_adjacency_pairs(["a"])

    assert result == []
    assert neo4j.calls == []


@pytest.mark.unit
def test_fetch_adjacency_pairs_forwards_namespace_to_query_layer() -> None:
    # SSOT-04 regression: the adapter previously dropped namespace entirely, so
    # MemoryQueryRepository's namespace-constrained Cypher was unreachable from
    # recall's adjacency scoring even though the repository itself supported it.
    neo4j = _StubNeo4jRepository(
        responses=[[{"source": "a", "target": "b", "weight": 1.0, "edge_uuid": "r-1"}]]
    )
    adapter = MemoryGraphAdapter(neo4j=neo4j)

    adapter.fetch_adjacency_pairs(["a", "b"], namespace="alpha")

    query, params = neo4j.calls[0]["query"], neo4j.calls[0]["params"]
    assert "coalesce(a.namespace, 'default') = $namespace" in query
    assert params["namespace"] == "alpha"


@pytest.mark.unit
def test_touch_retrieved_nodes_returns_count() -> None:
    neo4j = _StubNeo4jRepository(responses=[[{"touched": 2}]])
    adapter = MemoryGraphAdapter(neo4j=neo4j)

    result = adapter.touch_retrieved_nodes(["e-1", "e-2"])

    assert result == 2
    assert "SET n.last_accessed = datetime()" in neo4j.calls[0]["query"]


@pytest.mark.unit
def test_touch_retrieved_nodes_empty_returns_zero() -> None:
    neo4j = _StubNeo4jRepository()
    adapter = MemoryGraphAdapter(neo4j=neo4j)

    result = adapter.touch_retrieved_nodes([])

    assert result == 0
    assert neo4j.calls == []


@pytest.mark.unit
def test_fetch_flagged_memories_filters_to_user_flagged_rows() -> None:
    neo4j = _StubNeo4jRepository(
        responses=[[{"uuid": "e-1", "user_flagged": True}]]
    )
    adapter = MemoryGraphAdapter(neo4j=neo4j)

    result = adapter.fetch_flagged_memories(limit=7)

    assert len(result) == 1
    assert result[0]["uuid"] == "e-1"
    assert "coalesce(n.user_flagged, false) = true" in neo4j.calls[0]["query"]
    assert "n.bootstrap_scope IN $allowed_scopes" in neo4j.calls[0]["query"]
    assert neo4j.calls[0]["params"]["limit"] == 7
    assert neo4j.calls[0]["params"]["allowed_scopes"] == ["general"]


@pytest.mark.unit
def test_fetch_flagged_memory_bootstrap_version_hashes_sorted_uuids() -> None:
    neo4j = _StubNeo4jRepository(
        responses=[[{"uuids": ["z-uuid", "a-uuid"]}]]
    )
    adapter = MemoryGraphAdapter(neo4j=neo4j)

    version = adapter.fetch_flagged_memory_bootstrap_version()

    assert version.startswith("general:2:")
    assert len(version.rsplit(":", 1)[1]) == 16
    assert "collect(n.uuid) AS uuids" in neo4j.calls[0]["query"]


@pytest.mark.unit
def test_fetch_recent_memories_excludes_structure_and_scopes_namespace() -> None:
    neo4j = _StubNeo4jRepository(responses=[[{"uuid": "semantic-1"}]])
    adapter = MemoryGraphAdapter(neo4j=neo4j)

    rows = adapter.fetch_recent_memories(limit=4, namespace="archolith")

    assert rows == [{"uuid": "semantic-1"}]
    query = neo4j.calls[0]["query"]
    assert "WHERE (n:Entity OR n:Episodic)" in query
    assert "n.structure_role IS NULL" in query
    assert "coalesce(n.source, '') CONTAINS 'project-scan'" in query
    assert "'directory:'" in query
    assert "'file:'" in query
    assert "'project:'" in query
    assert "coalesce(n.namespace, 'default') = $namespace" in query
    assert neo4j.calls[0]["params"] == {
        "limit": 4,
        "namespace": "archolith",
    }


@pytest.mark.unit
def test_claim_pending_episode_sets_owner_and_lease() -> None:
    neo4j = _StubNeo4jRepository(
        responses=[
            [{"uuid": "episode-1", "processing_state": "PENDING", "processing_attempts": 0, "processing_error": None}],
            [{"uuid": "episode-1", "processing_owner": "worker-1", "processing_lease_expires_at": "later"}],
        ]
    )
    adapter = MemoryGraphAdapter(neo4j=neo4j)

    result = adapter.claim_pending_episode(
        "episode-1",
        max_attempts=3,
        worker_id="worker-1",
        lease_seconds=900,
    )

    assert result is not None
    assert neo4j.calls[1]["params"]["worker_id"] == "worker-1"
    assert neo4j.calls[1]["params"]["lease_seconds"] == 900
    assert neo4j.calls[1]["params"]["context_retry_attempts"] == 3
    assert neo4j.calls[1]["params"]["context_error_markers"] == [
        "cannot truncate prompt",
        "n_keep",
        "n_ctx",
        "available context window",
        "exceeds the available context window",
        "exceeds the available context",
    ]
    assert "n.processing_owner = $worker_id" in neo4j.calls[1]["query"]
    assert "n.processing_lease_expires_at = datetime() + duration({seconds: $lease_seconds})" in neo4j.calls[1]["query"]
    assert "coalesce(toInteger(n.processing_attempts), 0) < $context_retry_attempts" in neo4j.calls[1]["query"]
    assert "ANY(" in neo4j.calls[1]["query"]


@pytest.mark.unit
def test_reset_stale_enriching_episodes_returns_reset_count() -> None:
    neo4j = _StubNeo4jRepository(responses=[[{"reset": 2}]])
    adapter = MemoryGraphAdapter(neo4j=neo4j)

    result = adapter.reset_stale_enriching_episodes(max_attempts=3)

    assert result == 2
    assert neo4j.calls[0]["params"] == {"max_attempts": 3}
    assert "n.processing_state = 'ENRICHING'" in neo4j.calls[0]["query"]
    assert "n.processing_lease_expires_at < datetime()" in neo4j.calls[0]["query"]
    assert "coalesce(toInteger(n.processing_attempts), 0) >= $max_attempts" in neo4j.calls[0]["query"]
    assert "lease_expired_reset_attempts_exhausted" in neo4j.calls[0]["query"]
    assert "n.processing_lease_expires_at IS NULL" in neo4j.calls[0]["query"]


@pytest.mark.unit
def test_reset_orphaned_enriching_episodes_returns_reset_count() -> None:
    neo4j = _StubNeo4jRepository(responses=[[{"reset": 1}]])
    adapter = MemoryGraphAdapter(neo4j=neo4j)

    result = adapter.reset_orphaned_enriching_episodes(max_attempts=3)

    assert result == 1
    assert neo4j.calls[0]["params"] == {"max_attempts": 3}
    assert "n.processing_state = 'ENRICHING'" in neo4j.calls[0]["query"]
    assert "n.processing_owner IS NULL" in neo4j.calls[0]["query"]
    assert "orphaned_reset_attempts_exhausted" in neo4j.calls[0]["query"]


@pytest.mark.unit
def test_list_episode_processing_filters_by_state() -> None:
    neo4j = _StubNeo4jRepository(
        responses=[[{"uuid": "episode-1", "processing_state": "ENRICHING"}]]
    )
    adapter = MemoryGraphAdapter(neo4j=neo4j)

    result = adapter.list_episode_processing(processing_states=["ENRICHING"], limit=10)

    assert len(result) == 1
    assert result[0]["uuid"] == "episode-1"
    assert "n.processing_state IN $states" in neo4j.calls[0]["query"]
    assert neo4j.calls[0]["params"] == {"states": ["ENRICHING"], "limit": 10}


@pytest.mark.unit
def test_fetch_stale_enriching_episodes_includes_missing_leases() -> None:
    neo4j = _StubNeo4jRepository(
        responses=[[{"uuid": "episode-1", "processing_state": "ENRICHING"}]]
    )
    adapter = MemoryGraphAdapter(neo4j=neo4j)

    result = adapter.fetch_stale_enriching_episodes(include_missing_lease=True, limit=20)

    assert len(result) == 1
    assert result[0]["uuid"] == "episode-1"
    assert "$include_missing_lease = true" in neo4j.calls[0]["query"]
    assert neo4j.calls[0]["params"] == {"include_missing_lease": True, "limit": 20}


@pytest.mark.unit
def test_force_release_episode_lease_returns_true_when_row_released() -> None:
    neo4j = _StubNeo4jRepository(responses=[[{"released": 1}]])
    adapter = MemoryGraphAdapter(neo4j=neo4j)

    released = adapter.force_release_episode_lease("episode-1", max_attempts=3)

    assert released is True
    assert neo4j.calls[0]["params"] == {"uuid": "episode-1", "max_attempts": 3}
    assert "WHERE n.processing_state = 'ENRICHING'" in neo4j.calls[0]["query"]
    assert "n.processing_substage = CASE" in neo4j.calls[0]["query"]
    assert "manual_lease_release_attempts_exhausted" in neo4j.calls[0]["query"]


@pytest.mark.unit
def test_release_worker_episode_leases_returns_count() -> None:
    neo4j = _StubNeo4jRepository(responses=[[{"released": 2}]])
    adapter = MemoryGraphAdapter(neo4j=neo4j)

    result = adapter.release_worker_episode_leases("worker-1", max_attempts=3)

    assert result == 2
    assert neo4j.calls[0]["params"] == {"worker_id": "worker-1", "max_attempts": 3}
    assert "n.processing_owner = $worker_id" in neo4j.calls[0]["query"]
    assert "worker_shutdown_release_attempts_exhausted" in neo4j.calls[0]["query"]


@pytest.mark.unit
def test_increment_episode_llm_usage_updates_attempt_and_total() -> None:
    neo4j = _StubNeo4jRepository(responses=[[{"updated": 1}]])
    adapter = MemoryGraphAdapter(neo4j=neo4j)

    updated = adapter.increment_episode_llm_usage(
        "episode-1",
        task_delta=2,
        phase="started",
        kind="chat",
        model="test-model",
        endpoint="chat.completions.create",
        task="memory: graphiti add_episode",
    )

    assert updated is True
    params = neo4j.calls[0]["params"]
    assert params["episode_uuid"] == "episode-1"
    assert params["task_delta"] == 2
    assert params["substage"] == "llm_request_started"
    assert params["kind"] == "chat"
    assert params["model"] == "test-model"
    assert params["endpoint"] == "chat.completions.create"
    assert params["task"] == "memory: graphiti add_episode"
    query = neo4j.calls[0]["query"]
    assert "processing_llm_tasks_attempt" in query
    assert "processing_llm_tasks_total" in query
    assert "n.processing_substage = $substage" in query


@pytest.mark.unit
def test_delete_memory_returns_true_when_node_is_deleted() -> None:
    neo4j = _StubNeo4jRepository(responses=[[
        {"memory_touched": 1, "assertions_deleted": 0, "heads_deleted": 0, "repairs": []}
    ]])
    adapter = MemoryGraphAdapter(neo4j=neo4j)

    result = adapter.delete_memory("entity-123")

    assert result is True
    assert neo4j.calls[0]["params"]["node_uuid"] == "entity-123"
    assert neo4j.calls[0]["params"]["operation_id"]
    assert "ScalarProjectionRepair" in neo4j.calls[0]["query"]
    assert "deleted_by_user" in neo4j.calls[0]["query"]
    assert "DETACH DELETE" in neo4j.calls[0]["query"]
    assert "ADMITTED_ON" in neo4j.calls[0]["query"]


@pytest.mark.unit
def test_delete_memory_returns_false_when_no_node_found() -> None:
    neo4j = _StubNeo4jRepository(responses=[[
        {"memory_touched": 0, "assertions_deleted": 0, "heads_deleted": 0, "repairs": []}
    ]])
    adapter = MemoryGraphAdapter(neo4j=neo4j)

    result = adapter.delete_memory("nonexistent")

    assert result is False


@pytest.mark.unit
def test_delete_memory_returns_true_for_a_surfaced_assertion() -> None:
    neo4j = _StubNeo4jRepository(responses=[[
        {"memory_touched": 0, "assertions_deleted": 1, "heads_deleted": 1,
         "repairs": [{"subject_uuid": "ent-1", "namespace": "ns"}]}
    ], []])
    adapter = MemoryGraphAdapter(neo4j=neo4j)

    assert adapter.delete_memory("assertion-1") is True
    assert "a.assertion_id = $node_uuid" in neo4j.calls[0]["query"]
    assert "as_of" not in neo4j.calls[0]["params"]


@pytest.mark.unit
def test_find_completed_episode_artifact_returns_existing_graphiti_episode() -> None:
    neo4j = _StubNeo4jRepository(
        responses=[
            [{"resolved_episode_uuid": "episode-existing-1", "entity_uuids": ["entity-1", "entity-2"]}],
            [{"edge_uuids": ["edge-episode-1"]}],
            [{"edge_uuids": ["edge-entity-1", "edge-entity-2"]}],
        ]
    )
    adapter = MemoryGraphAdapter(neo4j=neo4j)

    result = adapter.find_completed_episode_artifact(
        anchor_uuid="pending-1",
        anchor_name="episode-session-1-pending-1",
    )

    assert result == {
        "resolved_episode_uuid": "episode-existing-1",
        "entity_uuids": ["entity-1", "entity-2"],
        "edge_uuids": ["edge-episode-1", "edge-entity-1", "edge-entity-2"],
    }
    assert neo4j.calls[0]["params"] == {
        "anchor_uuid": "pending-1",
        "anchor_name": "episode-session-1-pending-1",
    }


@pytest.mark.unit
def test_find_completed_episode_artifact_logs_and_skips_name_collisions(
    caplog: pytest.LogCaptureFixture,
) -> None:
    neo4j = _StubNeo4jRepository(
        responses=[
            [
                {"resolved_episode_uuid": "episode-existing-1", "entity_uuids": ["entity-1"]},
                {"resolved_episode_uuid": "episode-existing-2", "entity_uuids": ["entity-2"]},
            ],
        ]
    )
    adapter = MemoryGraphAdapter(neo4j=neo4j)

    with caplog.at_level("WARNING"):
        result = adapter.find_completed_episode_artifact(
            anchor_uuid="pending-1",
            anchor_name="episode-session-1-pending-1",
        )

    assert result is None
    assert "Multiple completed episode artifacts found" in caplog.text


@pytest.mark.unit
def test_compress_node_returns_true_when_node_updated() -> None:
    neo4j = _StubNeo4jRepository(responses=[[{"updated": 1}]])
    adapter = MemoryGraphAdapter(neo4j=neo4j)

    result = adapter.compress_node("entity-123", "short summary")

    assert result is True
    assert neo4j.calls[0]["params"]["node_uuid"] == "entity-123"
    assert neo4j.calls[0]["params"]["compressed_summary"] == "short summary"
    assert "SET n.original_content = coalesce(n.original_content, n.content)" in neo4j.calls[0]["query"]


@pytest.mark.unit
def test_fetch_node_freshness_returns_mapping() -> None:
    neo4j = _StubNeo4jRepository(
        responses=[[{"uuid": "entity-123", "freshness": "COMPRESSED"}]]
    )
    adapter = MemoryGraphAdapter(neo4j=neo4j)

    result = adapter.fetch_node_freshness(["entity-123"])

    assert result == {"entity-123": "COMPRESSED"}
    assert neo4j.calls[0]["params"]["uuids"] == ["entity-123"]
    assert "coalesce(n.freshness, 'ACTIVE')" in neo4j.calls[0]["query"]


@pytest.mark.unit
def test_complete_rehydration_returns_true_when_node_updated() -> None:
    neo4j = _StubNeo4jRepository(responses=[[{"updated": 1}]])
    adapter = MemoryGraphAdapter(neo4j=neo4j)

    result = adapter.complete_rehydration("entity-123", "merged summary")

    assert result is True
    assert neo4j.calls[0]["params"]["node_uuid"] == "entity-123"
    assert neo4j.calls[0]["params"]["updated_content"] == "merged summary"
    assert "n.freshness = 'ACTIVE'" in neo4j.calls[0]["query"]
    assert "n.last_accessed = datetime()" in neo4j.calls[0]["query"]


@pytest.mark.unit
def test_bridge_and_delete_returns_counts() -> None:
    neo4j = _StubNeo4jRepository(responses=[[{"edges_bridged": 2, "deleted": 1}]])
    adapter = MemoryGraphAdapter(neo4j=neo4j)

    result = adapter.bridge_and_delete("entity-123")

    assert result == {"edges_bridged": 2, "deleted": 1}
    assert neo4j.calls[0]["params"]["node_uuid"] == "entity-123"
    assert "MERGE (a)-[r:RELATES_TO]->(b)" in neo4j.calls[0]["query"]
    assert "DETACH DELETE n" in neo4j.calls[0]["query"]


@pytest.mark.unit
def test_cleanup_orphan_episodes_excludes_flagged_rows() -> None:
    neo4j = _StubNeo4jRepository(responses=[[{"deleted": 2}]])
    adapter = MemoryGraphAdapter(neo4j=neo4j)

    result = adapter.cleanup_orphan_episodes("session-1")

    assert result == 2
    assert neo4j.calls[0]["params"] == {"session_id": "session-1"}
    assert "coalesce(n.user_flagged, false) = false" in neo4j.calls[0]["query"]
    assert "n.processing_state = 'READY'" in neo4j.calls[0]["query"]


@pytest.mark.unit
def test_increment_edge_weight_returns_true_when_an_edge_is_updated() -> None:
    neo4j = _StubNeo4jRepository(responses=[[{"edges_updated": 1}]])
    adapter = MemoryGraphAdapter(neo4j=neo4j)

    result = adapter.increment_edge_weight("rel-123")

    assert result is True
    assert neo4j.calls[0]["params"] == {"edge_uuid": "rel-123"}
    assert "last_traversed = datetime()" in neo4j.calls[0]["query"]
    assert "ELSE 5.0" in neo4j.calls[0]["query"]


@pytest.mark.unit
def test_list_conflict_groups_applies_safe_limit_and_status_filter() -> None:
    neo4j = _StubNeo4jRepository(
        responses=[[{"group_id": "g-1", "created_at": "2026-03-10T00:00:00Z", "members": []}]]
    )
    adapter = MemoryGraphAdapter(neo4j=neo4j)

    rows = adapter.list_conflict_groups(status="unresolved", limit=999)

    assert len(rows) == 1
    assert rows[0]["group_id"] == "g-1"
    assert neo4j.calls[0]["params"] == {"status": "unresolved", "limit": 200}
    assert "MATCH (n:Entity)" in neo4j.calls[0]["query"]
    assert "collect({" in neo4j.calls[0]["query"]


@pytest.mark.unit
def test_list_conflict_pairs_wrapper_delegates_to_grouped_query() -> None:
    neo4j = _StubNeo4jRepository(
        responses=[[{"group_id": "g-2", "created_at": "2026-03-10T00:00:00Z", "members": []}]]
    )
    adapter = MemoryGraphAdapter(neo4j=neo4j)

    rows = adapter.list_conflict_pairs(status="unresolved", limit=5)

    assert len(rows) == 1
    assert rows[0]["group_id"] == "g-2"


@pytest.mark.unit
def test_resolve_conflict_group_keep_both_clears_group_and_sets_status() -> None:
    neo4j = _StubNeo4jRepository(responses=[
        [{"uuid": "m1"}, {"uuid": "m2"}, {"uuid": "m3"}],  # prefetch
        [{"resolved": 3}],  # keep_both SET
    ])
    adapter = MemoryGraphAdapter(neo4j=neo4j)

    result = adapter.resolve_conflict_group(
        "group-1",
        "keep_both",
        resolution_status="auto-resolved",
    )

    assert result == {
        "action": "keep_both",
        "group_id": "group-1",
        "resolved": 3,
        "removed_uuid": None,
        "removed_uuids": [],
        "bridged_edges": 0,
        "member_uuids": ["m1", "m2", "m3"],
    }
    assert neo4j.calls[1]["params"] == {
        "group_id": "group-1",
        "resolution_status": "auto-resolved",
    }
    assert "n.conflict_status = $resolution_status" in neo4j.calls[1]["query"]


@pytest.mark.unit
def test_resolve_conflict_group_replace_requires_keep_uuid() -> None:
    neo4j = _StubNeo4jRepository(responses=[
        [],  # prefetch (consumed before validation)
    ])
    adapter = MemoryGraphAdapter(neo4j=neo4j)

    with pytest.raises(ValueError, match="requires keep_uuid"):
        adapter.resolve_conflict_group("group-1", "replace")

    assert len(neo4j.calls) == 1  # prefetch only


@pytest.mark.unit
def test_resolve_conflict_group_replace_absorbs_content_resolves_and_bridges() -> None:
    neo4j = _StubNeo4jRepository(
        responses=[
            [{"uuid": "keep-1"}, {"uuid": "remove-1"}],  # prefetch
            [
                {"uuid": "keep-1", "scope": "PERSISTENT", "name": "Keep"},
                {"uuid": "remove-1", "scope": "PERSISTENT", "name": "Remove"},
            ],
            [{"content": "Keep text", "original_content": None}],
            [{"content": "Removed text"}],
            [],
            [{"removed_uuids": ["remove-1"]}],
            [{"resolved": 2}],
            [{"total_edges_bridged": 1}],
        ]
    )
    adapter = MemoryGraphAdapter(neo4j=neo4j)

    result = adapter.resolve_conflict_group(
        "group-1",
        "replace",
        keep_uuid="keep-1",
        remove_uuid="remove-1",
    )

    assert result == {
        "action": "replace",
        "group_id": "group-1",
        "resolved": 2,
        "removed_uuid": "remove-1",
        "removed_uuids": ["remove-1"],
        "bridged_edges": 1,
        "member_uuids": ["keep-1", "remove-1"],
    }
    assert len(neo4j.calls) == 8
    absorption_call = neo4j.calls[4]
    assert absorption_call["params"]["uuid"] == "keep-1"
    assert "Absorbed from superseded memory (remove-1)" in absorption_call["params"]["content"]
    assert absorption_call["params"]["original"] == "Keep text"
    assert "n.original_content = coalesce(n.original_content, $original)" in absorption_call["query"]
    gone_call = neo4j.calls[5]
    assert gone_call["params"] == {
        "group_id": "group-1",
        "remove_uuids": ["remove-1"],
        "resolution_status": "resolved",
    }
    assert "n.freshness = 'GONE'" in gone_call["query"]
    assert "n.conflict_status = $resolution_status" in gone_call["query"]


@pytest.mark.unit
def test_resolve_conflict_group_replace_without_remove_uuid_gones_all_non_keep() -> None:
    neo4j = _StubNeo4jRepository(
        responses=[
            [{"uuid": "keep-1"}, {"uuid": "remove-1"}, {"uuid": "remove-2"}],  # prefetch
            [
                {"uuid": "keep-1", "scope": "PERSISTENT", "name": "Keep"},
                {"uuid": "remove-1", "scope": "PERSISTENT", "name": "Remove One"},
                {"uuid": "remove-2", "scope": "PERSISTENT", "name": "Remove Two"},
            ],
            [{"content": "Keep text", "original_content": None}],
            [{"content": "Removed text one"}],
            [],
            [{"content": "Removed text two"}],
            [],
            [{"removed_uuids": ["remove-1", "remove-2"]}],
            [{"resolved": 1}],
            [{"total_edges_bridged": 3}],
        ]
    )
    adapter = MemoryGraphAdapter(neo4j=neo4j)

    result = adapter.resolve_conflict_group(
        "group-1",
        "replace",
        keep_uuid="keep-1",
    )

    assert result["removed_uuids"] == ["remove-1", "remove-2"]
    assert result["removed_uuid"] == "remove-1"
    assert result["bridged_edges"] == 3
    gone_call = next(
        call for call in neo4j.calls if "AND n.uuid IN $remove_uuids" in call["query"]
    )
    assert gone_call["params"] == {
        "group_id": "group-1",
        "remove_uuids": ["remove-1", "remove-2"],
        "resolution_status": "resolved",
    }


@pytest.mark.unit
def test_resolve_conflict_group_blocks_promoted_removal_by_default() -> None:
    neo4j = _StubNeo4jRepository(
        responses=[
            [{"uuid": "keep-1"}, {"uuid": "remove-1"}],  # prefetch
            [
                {"uuid": "keep-1", "scope": "PERSISTENT", "name": "Keep"},
                {"uuid": "remove-1", "scope": "PROMOTED", "name": "Pinned Fact"},
            ],
        ]
    )
    adapter = MemoryGraphAdapter(neo4j=neo4j)

    with pytest.raises(ValueError, match="Pinned Fact \\(remove-1\\)"):
        adapter.resolve_conflict_group(
            "group-1",
            "replace",
            keep_uuid="keep-1",
            remove_uuid="remove-1",
        )


@pytest.mark.unit
def test_resolve_conflict_group_allows_promoted_removal_with_override() -> None:
    neo4j = _StubNeo4jRepository(
        responses=[
            [{"uuid": "keep-1"}, {"uuid": "remove-1"}],  # prefetch
            [
                {"uuid": "keep-1", "scope": "PERSISTENT", "name": "Keep"},
                {"uuid": "remove-1", "scope": "PROMOTED", "name": "Pinned Fact"},
            ],
            [{"content": "Keep text", "original_content": None}],
            [{"content": "Promoted text"}],
            [],
            [{"removed_uuids": ["remove-1"]}],
            [{"resolved": 1}],
            [{"total_edges_bridged": 0}],
        ]
    )
    adapter = MemoryGraphAdapter(neo4j=neo4j)

    result = adapter.resolve_conflict_group(
        "group-1",
        "replace",
        keep_uuid="keep-1",
        remove_uuid="remove-1",
        allow_promoted_removal=True,
    )

    assert result["removed_uuids"] == ["remove-1"]
    assert result["resolved"] == 1


@pytest.mark.unit
def test_resolve_conflict_group_discard_new_skips_absorption_on_high_overlap() -> None:
    text = "scheduler acquire uses localhost 8082 and llama endpoint"
    neo4j = _StubNeo4jRepository(
        responses=[
            [{"uuid": "keep-1"}, {"uuid": "remove-1"}],  # prefetch
            [
                {"uuid": "keep-1", "scope": "PERSISTENT", "name": "Keep"},
                {"uuid": "remove-1", "scope": "PERSISTENT", "name": "Remove"},
            ],
            [{"content": text, "original_content": None}],
            [{"content": text}],
            [{"removed_uuids": ["remove-1"]}],
            [{"resolved": 1}],
            [],
            [{"total_edges_bridged": 0}],
        ]
    )
    adapter = MemoryGraphAdapter(neo4j=neo4j)

    result = adapter.resolve_conflict_group(
        "group-1",
        "discard_new",
        keep_uuid="keep-1",
        remove_uuid="remove-1",
    )

    assert result["action"] == "discard_new"
    assert all("SET n.content = $content" not in call["query"] for call in neo4j.calls)
    assert any("n.pending_review = true" in call["query"] for call in neo4j.calls)


@pytest.mark.unit
def test_resolve_conflict_group_discard_new_absorbs_on_low_overlap_and_marks_review() -> None:
    neo4j = _StubNeo4jRepository(
        responses=[
            [{"uuid": "keep-1"}, {"uuid": "remove-1"}],  # prefetch
            [
                {"uuid": "keep-1", "scope": "PERSISTENT", "name": "Keep"},
                {"uuid": "remove-1", "scope": "PERSISTENT", "name": "Remove"},
            ],
            [{"content": "alpha beta gamma", "original_content": None}],
            [{"content": "different tokens only"}],
            [],
            [{"removed_uuids": ["remove-1"]}],
            [{"resolved": 1}],
            [],
            [{"total_edges_bridged": 1}],
        ]
    )
    adapter = MemoryGraphAdapter(neo4j=neo4j)

    result = adapter.resolve_conflict_group(
        "group-1",
        "discard_new",
        keep_uuid="keep-1",
        remove_uuid="remove-1",
    )

    assert result["bridged_edges"] == 1
    assert any("SET n.content = $content" in call["query"] for call in neo4j.calls)
    assert any("n.pending_review = true" in call["query"] for call in neo4j.calls)
