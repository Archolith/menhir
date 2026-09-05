"""Unit tests for RecallService orchestration with stubs."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace

import pytest

from menhir.domain.recall import QueryPreset, RecallResult
from menhir.domain.self_identity import self_uuid_for_namespace
from menhir.domain.retrieval_tuning import (
    CandidateSource,
    RetrievalScoreKind,
    RetrievalTuningConfig,
)
from menhir.services.recall_service import (
    RecallService,
    _blend_oracle_order,
    _oracle_similarity,
)
from menhir.services.scoring_service import ScoringService


def _meta(uuid: str, name: str) -> dict:
    return {
        "uuid": uuid,
        "name": name,
        "scope": "PERSISTENT",
        "type": "SEMANTIC",
        "content": "content",
        "summary": None,
        "last_accessed": datetime.now(timezone.utc),
        "edge_count": 1,
        "sharpness": 0.1,
        "freshness": "ACTIVE",
        "user_flagged": False,
    }


def _build_recall_service(stub_graphiti_client, stub_memory_graph_adapter, **kwargs):
    return RecallService(
        graphiti_client=stub_graphiti_client,
        graph_adapter=stub_memory_graph_adapter,
        scoring_service=ScoringService(),
        **kwargs,
    )


@pytest.mark.unit
def test_projection_recall_eligibility_requires_explicit_true() -> None:
    from menhir.services.recall_pipeline import _projection_is_recall_eligible

    assert _projection_is_recall_eligible({"recall_eligible": True}) is True
    assert _projection_is_recall_eligible({}) is False
    assert _projection_is_recall_eligible({"recall_eligible": False}) is False
    assert _projection_is_recall_eligible({"recall_eligible": 1}) is False


def _setup_search_and_metadata(stub_graphiti_client, stub_memory_graph_adapter, **overrides):
    """Configure stubs with default search results and metadata."""
    scope = overrides.get("scope", "PERSISTENT")
    freshness = overrides.get("freshness", "ACTIVE")
    last_accessed = overrides.get("last_accessed", datetime.now(timezone.utc) - timedelta(hours=1))

    stub_graphiti_client.search_scored_results = [
        ("entity-1", "Test Entity", 0.85),
        ("entity-2", "Another Entity", 0.65),
    ]
    stub_memory_graph_adapter.candidate_metadata = [
        {
            "uuid": "entity-1",
            "name": "Test Entity",
            "scope": scope,
            "type": "SEMANTIC",
            "content": "test content",
            "summary": None,
            "last_accessed": last_accessed,
            "edge_count": 5,
            "sharpness": 0.5,
            "freshness": freshness,
            "user_flagged": False,
        },
        {
            "uuid": "entity-2",
            "name": "Another Entity",
            "scope": scope,
            "type": "SEMANTIC",
            "content": "other content",
            "summary": None,
            "last_accessed": last_accessed,
            "edge_count": 2,
            "sharpness": 0.3,
            "freshness": freshness,
            "user_flagged": False,
        },
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recall_calls_graphiti_search_scored(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    _setup_search_and_metadata(stub_graphiti_client, stub_memory_graph_adapter)
    svc = _build_recall_service(stub_graphiti_client, stub_memory_graph_adapter)

    await svc.recall("test query")

    assert len(stub_graphiti_client.search_scored_calls) == 1
    assert stub_graphiti_client.search_scored_calls[0]["query"] == "test query"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recall_filters_session_nodes_by_default(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    _setup_search_and_metadata(stub_graphiti_client, stub_memory_graph_adapter, scope="SESSION")
    svc = _build_recall_service(stub_graphiti_client, stub_memory_graph_adapter)

    result = await svc.recall("test query")

    assert len(result.results) == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recall_includes_session_nodes_when_requested(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    _setup_search_and_metadata(stub_graphiti_client, stub_memory_graph_adapter, scope="SESSION")
    svc = _build_recall_service(stub_graphiti_client, stub_memory_graph_adapter)

    result = await svc.recall("test query", include_session=True)

    assert len(result.results) == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recall_excludes_gone_nodes(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    _setup_search_and_metadata(stub_graphiti_client, stub_memory_graph_adapter, freshness="GONE")
    svc = _build_recall_service(stub_graphiti_client, stub_memory_graph_adapter)

    result = await svc.recall("test query")

    assert len(result.results) == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recall_excludes_candidate_nodes(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    # CANDIDATE = human-review tier; must never surface in recall until approved.
    _setup_search_and_metadata(stub_graphiti_client, stub_memory_graph_adapter, scope="CANDIDATE")
    svc = _build_recall_service(stub_graphiti_client, stub_memory_graph_adapter)

    result = await svc.recall("test query")

    assert len(result.results) == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recall_candidate_exclusion_is_unconditional(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    # Unlike SESSION, CANDIDATE stays excluded even with include_session=True.
    _setup_search_and_metadata(stub_graphiti_client, stub_memory_graph_adapter, scope="CANDIDATE")
    svc = _build_recall_service(stub_graphiti_client, stub_memory_graph_adapter)

    result = await svc.recall("test query", include_session=True)

    assert len(result.results) == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recall_excludes_view_whose_durable_provenance_is_not_live(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    """A UUID receipt without its evidence node is not a recallable memory."""
    _setup_search_and_metadata(stub_graphiti_client, stub_memory_graph_adapter)
    stub_memory_graph_adapter.candidate_metadata[0].update({
        "is_view": True,
        "view_current": True,
        "episode_uuids": ["deleted-evidence"],
        "view_provenance_live": False,
    })
    svc = _build_recall_service(stub_graphiti_client, stub_memory_graph_adapter)

    result = await svc.recall("test query")

    assert [row.uuid for row in result.results] == ["entity-2"]


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "override",
    [
        {"view_class": "METRIC"},
        {"view_audience": "OPERATOR"},
        {"view_current": False},
        {"retired": True},
        {"view_provenance_live": False},
    ],
    ids=["non-fact", "operator", "noncurrent", "retired", "provenance-mismatch"],
)
async def test_recall_view_contract_is_fail_closed_even_with_superseded_opt_in(
    stub_graphiti_client, stub_memory_graph_adapter, override
) -> None:
    _setup_search_and_metadata(stub_graphiti_client, stub_memory_graph_adapter)
    view_meta = {
        "is_view": True,
        "view_kind": "counter",
        "view_class": "FACT",
        "view_audience": "RECALL",
        "view_current": True,
        "retired": False,
        "view_provenance_live": True,
    }
    view_meta.update(override)
    stub_memory_graph_adapter.candidate_metadata[0].update(view_meta)
    svc = _build_recall_service(stub_graphiti_client, stub_memory_graph_adapter)

    result = await svc.recall("test query", include_superseded=True)

    assert [row.uuid for row in result.results] == ["entity-2"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recall_excludes_structural_nodes(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    # Structural graph nodes (project-scan Directory/File/Project entities) carry a
    # structure_role. They exist for query_structure, never semantic recall, but leak in
    # via BM25 token collisions. Recall must drop them while keeping the semantic neighbor.
    stub_graphiti_client.search_scored_results = [
        ("dir-1", "rules", 0.85),
        ("entity-2", "Real Memory", 0.65),
    ]
    stub_memory_graph_adapter.candidate_metadata = [
        {
            "uuid": "dir-1",
            "name": "rules",
            "scope": "PERSISTENT",
            "type": "SEMANTIC",
            "content": "Directory: .continue/rules",
            "summary": None,
            "last_accessed": datetime.now(timezone.utc),
            "edge_count": 1,
            "sharpness": 0.1,
            "freshness": "ACTIVE",
            "user_flagged": False,
            "structure_role": "directory",
        },
        {
            "uuid": "entity-2",
            "name": "Real Memory",
            "scope": "PERSISTENT",
            "type": "SEMANTIC",
            "content": "a genuine semantic memory",
            "summary": None,
            "last_accessed": datetime.now(timezone.utc),
            "edge_count": 2,
            "sharpness": 0.3,
            "freshness": "ACTIVE",
            "user_flagged": False,
            "structure_role": None,
        },
    ]
    svc = _build_recall_service(stub_graphiti_client, stub_memory_graph_adapter)

    result = await svc.recall("test query")

    returned = {r.uuid for r in result.results}
    assert "dir-1" not in returned
    assert "entity-2" in returned


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recall_truncates_to_limit(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    _setup_search_and_metadata(stub_graphiti_client, stub_memory_graph_adapter)
    svc = _build_recall_service(stub_graphiti_client, stub_memory_graph_adapter)

    result = await svc.recall("test query", limit=1)

    assert len(result.results) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recall_touches_retrieved_nodes(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    _setup_search_and_metadata(stub_graphiti_client, stub_memory_graph_adapter)
    svc = _build_recall_service(stub_graphiti_client, stub_memory_graph_adapter)

    result = await svc.recall("test query")

    assert stub_memory_graph_adapter.touch_count == len(result.results)
    assert result.nodes_touched == len(result.results)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recall_increments_edge_weights_on_traversal(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    _setup_search_and_metadata(stub_graphiti_client, stub_memory_graph_adapter)
    stub_memory_graph_adapter.adjacency_pairs = [
        {"source": "entity-1", "target": "entity-2", "weight": 1.5, "edge_uuid": "edge-1"},
    ]
    svc = _build_recall_service(stub_graphiti_client, stub_memory_graph_adapter)

    await svc.recall("test query")

    assert "edge-1" in stub_memory_graph_adapter.weighted_edges


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recall_does_not_increment_edges_for_filtered_candidates(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    """Edge weights should only be incremented for edges fully inside returned results."""
    stub_graphiti_client.search_scored_results = [
        ("entity-1", "Returned Entity", 0.9),
        ("entity-2", "Filtered Entity", 0.5),
    ]
    stub_memory_graph_adapter.candidate_metadata = [
        {
            "uuid": "entity-1",
            "name": "Returned Entity",
            "scope": "PERSISTENT",
            "type": "SEMANTIC",
            "content": "content",
            "summary": None,
            "last_accessed": datetime.now(timezone.utc),
            "edge_count": 5,
            "sharpness": 0.5,
            "freshness": "ACTIVE",
            "user_flagged": False,
        },
        {
            "uuid": "entity-2",
            "name": "Filtered Entity",
            "scope": "PERSISTENT",
            "type": "SEMANTIC",
            "content": "content",
            "summary": None,
            "last_accessed": datetime.now(timezone.utc),
            "edge_count": 1,
            "sharpness": 0.1,
            "freshness": "ACTIVE",
            "user_flagged": False,
        },
    ]
    # edge-between connects both candidates; edge-filtered connects only filtered ones
    stub_memory_graph_adapter.adjacency_pairs = [
        {"source": "entity-1", "target": "entity-2", "weight": 1.0, "edge_uuid": "edge-between"},
    ]
    svc = _build_recall_service(stub_graphiti_client, stub_memory_graph_adapter)

    result = await svc.recall("test query", limit=1)

    assert len(result.results) == 1
    assert result.results[0].uuid == "entity-1"
    assert "edge-between" not in stub_memory_graph_adapter.weighted_edges


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recall_filters_candidates_before_fetching_adjacency(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    stub_graphiti_client.search_scored_results = [
        ("entity-1", "Persistent Entity", 0.8),
        ("entity-2", "Session Entity", 0.7),
    ]
    stub_memory_graph_adapter.candidate_metadata = [
        {
            "uuid": "entity-1",
            "name": "Persistent Entity",
            "scope": "PERSISTENT",
            "type": "SEMANTIC",
            "content": "content",
            "summary": None,
            "last_accessed": datetime.now(timezone.utc),
            "edge_count": 3,
            "sharpness": 0.2,
            "freshness": "ACTIVE",
            "user_flagged": False,
        },
        {
            "uuid": "entity-2",
            "name": "Session Entity",
            "scope": "SESSION",
            "type": "SEMANTIC",
            "content": "content",
            "summary": None,
            "last_accessed": datetime.now(timezone.utc),
            "edge_count": 3,
            "sharpness": 0.2,
            "freshness": "ACTIVE",
            "user_flagged": False,
        },
    ]
    svc = _build_recall_service(stub_graphiti_client, stub_memory_graph_adapter)

    await svc.recall("test query", include_session=False)

    assert stub_memory_graph_adapter.adjacency_calls == [
        {"candidate_uuids": ["entity-1"], "context_uuids": [], "namespace": None}
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recall_returns_recall_result_with_explainability(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    _setup_search_and_metadata(stub_graphiti_client, stub_memory_graph_adapter)
    svc = _build_recall_service(stub_graphiti_client, stub_memory_graph_adapter)

    result = await svc.recall("test query")

    assert isinstance(result, RecallResult)
    assert result.query == "test query"
    assert result.preset == "knowledge"
    assert result.candidates_evaluated > 0
    for scored in result.results:
        b = scored.breakdown
        assert b.semantic_similarity >= 0
        assert b.preset == "knowledge"
        assert b.alpha >= 0
        assert b.beta >= 0
        assert b.gamma >= 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recall_uses_default_knowledge_preset(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    _setup_search_and_metadata(stub_graphiti_client, stub_memory_graph_adapter)
    svc = _build_recall_service(stub_graphiti_client, stub_memory_graph_adapter)

    result = await svc.recall("test query")

    assert result.preset == "knowledge"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recall_prefers_summary_for_knowledge_preset(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    _setup_search_and_metadata(stub_graphiti_client, stub_memory_graph_adapter)
    stub_memory_graph_adapter.candidate_metadata[0]["content"] = "full content should be avoided"
    stub_memory_graph_adapter.candidate_metadata[0]["summary"] = "short summary"
    svc = _build_recall_service(stub_graphiti_client, stub_memory_graph_adapter)

    result = await svc.recall("test query", preset=QueryPreset.KNOWLEDGE)

    assert result.results[0].content == "short summary"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recall_prefers_full_content_for_non_knowledge_presets(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    _setup_search_and_metadata(stub_graphiti_client, stub_memory_graph_adapter)
    stub_memory_graph_adapter.candidate_metadata[0]["content"] = "full content"
    stub_memory_graph_adapter.candidate_metadata[0]["summary"] = "short summary"
    svc = _build_recall_service(stub_graphiti_client, stub_memory_graph_adapter)

    result = await svc.recall("test query", preset=QueryPreset.RECENT)

    assert result.results[0].content == "full content"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recall_passes_context_node_ids_for_adjacency(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    _setup_search_and_metadata(stub_graphiti_client, stub_memory_graph_adapter)
    svc = _build_recall_service(stub_graphiti_client, stub_memory_graph_adapter)

    await svc.recall("test query", context_node_ids=["ctx-1", "ctx-2"])

    # The service should still run without error with context node ids
    # (adjacency pairs are empty in stub, which is fine)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_enforce_mode_removes_canonical_self_from_adjacency_context(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    namespace = "tenant-a"
    canonical_uuid = self_uuid_for_namespace(namespace)
    _setup_search_and_metadata(stub_graphiti_client, stub_memory_graph_adapter)
    candidate_rows = list(stub_memory_graph_adapter.candidate_metadata)
    for row in candidate_rows:
        row["namespace"] = namespace
    context_rows = [
        {
            **_meta(canonical_uuid, "user"),
            "namespace": namespace,
            "is_self": True,
            "entity_role": "self",
        },
        {**_meta("ctx-ordinary", "Project"), "namespace": namespace},
    ]

    def metadata_for(node_uuids):
        return context_rows if canonical_uuid in node_uuids else candidate_rows

    stub_memory_graph_adapter.fetch_candidate_metadata = metadata_for
    stub_memory_graph_adapter.canonical_self_binding_mode = "enforce"
    svc = _build_recall_service(stub_graphiti_client, stub_memory_graph_adapter)

    await svc.recall(
        "test query",
        namespace=namespace,
        context_node_ids=[canonical_uuid, "ctx-ordinary"],
    )

    assert stub_memory_graph_adapter.adjacency_calls[-1]["context_uuids"] == [
        "ctx-ordinary"
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_confirmed_self_edge_uses_exact_fact_in_pointer_mode(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    edge_calls = []

    async def search_edges_scored(query, **kwargs):
        edge_calls.append((query, kwargs))
        return [{
            "uuid": "signed-edge",
            "fact": "user owns 25 postcards",
            "score": 2.0,
            "source_node_uuid": self_uuid_for_namespace("tenant-a"),
            "target_node_uuid": "postcards",
            "canonical_self_authorized": True,
            "created_at": None,
            "valid_at": None,
            "invalid_at": None,
            "expired_at": None,
        }]

    stub_graphiti_client.search_edges_scored = search_edges_scored
    stub_memory_graph_adapter.canonical_self_binding_mode = "enforce"
    svc = _build_recall_service(stub_graphiti_client, stub_memory_graph_adapter)
    tuning = RetrievalTuningConfig(
        enable_fact_edges=True,
        fact_edge_mode="pointer",
        fact_edge_k=10,
    )

    result = await svc.recall(
        "what did I own previously?",
        namespace="tenant-a",
        tuning=tuning,
    )

    assert edge_calls
    assert result.candidates_evaluated == 1
    assert [row.uuid for row in result.results] == ["signed-edge"]
    assert result.results[0].content == "user owns 25 postcards"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recall_empty_search_returns_empty_result(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    stub_graphiti_client.search_scored_results = []
    svc = _build_recall_service(stub_graphiti_client, stub_memory_graph_adapter)

    result = await svc.recall("nothing here")

    assert result.results == []
    assert result.candidates_evaluated == 0
    assert result.nodes_touched == 0
    assert result.search_error is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recall_reports_search_failure_as_degraded_not_zero_match(
    stub_graphiti_client, stub_memory_graph_adapter, caplog
) -> None:
    async def fail_search(*args, **kwargs):
        raise AttributeError("'NoneType' object has no attribute 'replace'")

    stub_graphiti_client.search_scored = fail_search
    svc = _build_recall_service(stub_graphiti_client, stub_memory_graph_adapter)

    result = await svc.recall("yawn.rip vps")

    assert result.results == []
    assert result.search_error == "graphiti_search_unavailable:AttributeError"
    assert "not a confirmed zero-match" in (result.note or "")
    assert "query='yawn.rip vps'" in caplog.text
    assert any(record.exc_info is not None for record in caplog.records)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recall_skips_one_malformed_metadata_candidate(
    stub_graphiti_client, stub_memory_graph_adapter, caplog
) -> None:
    _setup_search_and_metadata(stub_graphiti_client, stub_memory_graph_adapter)
    stub_memory_graph_adapter.candidate_metadata[1]["edge_count"] = "not-an-integer"
    svc = _build_recall_service(stub_graphiti_client, stub_memory_graph_adapter)

    result = await svc.recall("test query", update_access=False)

    assert [memory.uuid for memory in result.results] == ["entity-1"]
    assert result.search_error is None
    assert "skipped malformed candidate metadata uuid='entity-2'" in caplog.text


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recall_survives_adjacency_backend_failure(
    stub_graphiti_client, stub_memory_graph_adapter, monkeypatch, caplog
) -> None:
    _setup_search_and_metadata(stub_graphiti_client, stub_memory_graph_adapter)

    def _fail_adjacency(*args, **kwargs):
        raise RuntimeError("adjacency unavailable")

    monkeypatch.setattr(
        stub_memory_graph_adapter, "fetch_adjacency_pairs", _fail_adjacency
    )
    svc = _build_recall_service(stub_graphiti_client, stub_memory_graph_adapter)

    result = await svc.recall("test query", update_access=False)

    assert {memory.uuid for memory in result.results} == {"entity-1", "entity-2"}
    assert "continuing without adjacency" in caplog.text


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recall_skips_one_malformed_adjacency_row(
    stub_graphiti_client, stub_memory_graph_adapter, caplog
) -> None:
    _setup_search_and_metadata(stub_graphiti_client, stub_memory_graph_adapter)
    stub_memory_graph_adapter.adjacency_pairs = [
        {"source": "entity-1", "target": "entity-2", "weight": "not-a-number"},
        {"source": "entity-1", "target": "entity-2", "weight": 0.5, "edge_uuid": "e2"},
    ]
    svc = _build_recall_service(stub_graphiti_client, stub_memory_graph_adapter)

    result = await svc.recall("test query", update_access=False)

    assert {memory.uuid for memory in result.results} == {"entity-1", "entity-2"}
    assert "skipped malformed adjacency row" in caplog.text


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recall_survives_post_recall_write_failure(
    stub_graphiti_client, stub_memory_graph_adapter, monkeypatch, caplog
) -> None:
    _setup_search_and_metadata(stub_graphiti_client, stub_memory_graph_adapter)

    def _fail_touch(*args, **kwargs):
        raise RuntimeError("write unavailable")

    monkeypatch.setattr(stub_memory_graph_adapter, "touch_retrieved_nodes", _fail_touch)
    svc = _build_recall_service(stub_graphiti_client, stub_memory_graph_adapter)

    result = await svc.recall("test query")

    assert {memory.uuid for memory in result.results} == {"entity-1", "entity-2"}
    assert result.nodes_touched == 0
    assert "returning results unchanged" in caplog.text


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recall_waits_for_relevant_pending_episode(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    _setup_search_and_metadata(stub_graphiti_client, stub_memory_graph_adapter)
    stub_memory_graph_adapter.pending_episode_rows["pending-1"] = {
        "uuid": "pending-1",
        "name": "pending memory",
        "content": "test query pending memory",
        "scope": "SESSION",
        "processing_state": "PENDING",
    }

    class StubIngestService:
        waits: list[str] = []

        async def wait_for_episode_processing(self, episode_uuid: str, *, timeout_s: float):
            self.waits.append(episode_uuid)
            stub_memory_graph_adapter.pending_episode_rows[episode_uuid]["processing_state"] = "READY"
            return dict(stub_memory_graph_adapter.pending_episode_rows[episode_uuid])

    svc = RecallService(
        graphiti_client=stub_graphiti_client,
        graph_adapter=stub_memory_graph_adapter,
        scoring_service=ScoringService(),
        ingest_service=StubIngestService(),
    )

    await svc.recall("test query", wait_for_pending=True)

    assert svc.ingest_service.waits == ["pending-1"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recall_ignores_unrelated_pending_episode(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    _setup_search_and_metadata(stub_graphiti_client, stub_memory_graph_adapter)
    stub_memory_graph_adapter.pending_episode_rows["pending-1"] = {
        "uuid": "pending-1",
        "name": "different memory",
        "content": "unrelated content",
        "scope": "SESSION",
        "processing_state": "PENDING",
    }

    class StubIngestService:
        waits: list[str] = []

        async def wait_for_episode_processing(self, episode_uuid: str, *, timeout_s: float):
            self.waits.append(episode_uuid)
            return dict(stub_memory_graph_adapter.pending_episode_rows[episode_uuid])

    svc = RecallService(
        graphiti_client=stub_graphiti_client,
        graph_adapter=stub_memory_graph_adapter,
        scoring_service=ScoringService(),
        ingest_service=StubIngestService(),
    )

    await svc.recall("test query", wait_for_pending=True)

    assert svc.ingest_service.waits == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recall_returns_pending_fallback_when_wait_times_out(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    stub_graphiti_client.search_scored_results = []
    stub_memory_graph_adapter.pending_episode_rows["pending-1"] = {
        "uuid": "pending-1",
        "name": "pending memory",
        "content": "remember this pending fact",
        "scope": "SESSION",
        "processing_state": "PENDING",
    }

    class StubIngestService:
        waits: list[str] = []

        async def wait_for_episode_processing(self, episode_uuid: str, *, timeout_s: float):
            self.waits.append(episode_uuid)
            return dict(stub_memory_graph_adapter.pending_episode_rows[episode_uuid])

    svc = RecallService(
        graphiti_client=stub_graphiti_client,
        graph_adapter=stub_memory_graph_adapter,
        scoring_service=ScoringService(),
        ingest_service=StubIngestService(),
    )

    result = await svc.recall(
        "pending fact",
        wait_for_pending=True,
        include_session=True,
        pending_wait_timeout_s=0.01,
    )

    assert len(result.results) == 1
    assert result.results[0].memory_type == "EPISODIC_PENDING"
    assert result.results[0].uuid == "pending-1"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recall_prepends_pending_fallback_even_when_search_has_results(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    _setup_search_and_metadata(stub_graphiti_client, stub_memory_graph_adapter)
    stub_memory_graph_adapter.pending_episode_rows["pending-1"] = {
        "uuid": "pending-1",
        "name": "pending memory",
        "content": "miso lantern bridge",
        "scope": "SESSION",
        "processing_state": "PENDING",
    }

    class StubIngestService:
        async def wait_for_episode_processing(self, episode_uuid: str, *, timeout_s: float):
            return dict(stub_memory_graph_adapter.pending_episode_rows[episode_uuid])

    svc = RecallService(
        graphiti_client=stub_graphiti_client,
        graph_adapter=stub_memory_graph_adapter,
        scoring_service=ScoringService(),
        ingest_service=StubIngestService(),
    )

    result = await svc.recall(
        "miso lantern",
        wait_for_pending=True,
        include_session=True,
        limit=3,
        pending_wait_timeout_s=0.01,
    )

    assert result.results[0].memory_type == "EPISODIC_PENDING"
    assert result.results[0].uuid == "pending-1"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recall_merges_linked_entities_from_just_ready_pending_episode(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    _setup_search_and_metadata(stub_graphiti_client, stub_memory_graph_adapter)
    stub_memory_graph_adapter.pending_episode_rows["pending-1"] = {
        "uuid": "pending-1",
        "name": "pending memory",
        "content": "miso lantern bridge",
        "scope": "SESSION",
        "processing_state": "PENDING",
        "resolved_episode_uuid": "resolved-episode-1",
        "linked_entity_uuids": ["entity-pending"],
    }
    # The code looks up linked entities by resolved_episode_uuid, so we need
    # an entry keyed by that UUID with the linked entity list.
    stub_memory_graph_adapter.pending_episode_rows["resolved-episode-1"] = {
        "uuid": "resolved-episode-1",
        "linked_entity_uuids": ["entity-pending"],
    }
    stub_memory_graph_adapter.candidate_metadata.append(
        {
            "uuid": "entity-pending",
            "name": "Canary Memory",
            "scope": "SESSION",
            "type": "SEMANTIC",
            "content": "miso lantern bridge canary memory",
            "summary": None,
            "last_accessed": datetime.now(timezone.utc),
            "edge_count": 1,
            "sharpness": 0.2,
            "freshness": "ACTIVE",
            "user_flagged": False,
        }
    )

    class StubIngestService:
        async def wait_for_episode_processing(self, episode_uuid: str, *, timeout_s: float):
            row = stub_memory_graph_adapter.pending_episode_rows[episode_uuid]
            row["processing_state"] = "READY"
            return dict(row)

    svc = RecallService(
        graphiti_client=stub_graphiti_client,
        graph_adapter=stub_memory_graph_adapter,
        scoring_service=ScoringService(),
        ingest_service=StubIngestService(),
    )

    result = await svc.recall(
        "miso lantern",
        wait_for_pending=True,
        include_session=True,
        limit=3,
    )

    assert any(row.uuid == "entity-pending" for row in result.results)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recall_schedules_rehydration_for_compressed_results(
    stub_graphiti_client, stub_memory_graph_adapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_search_and_metadata(stub_graphiti_client, stub_memory_graph_adapter, freshness="COMPRESSED")
    scheduled: list[tuple[str, str | None]] = []
    svc = _build_recall_service(
        stub_graphiti_client,
        stub_memory_graph_adapter,
        lifecycle_service=object(),
    )

    monkeypatch.setattr(
        svc,
        "_schedule_rehydration",
        lambda node_uuid, *, source_node_uuid=None: scheduled.append((node_uuid, source_node_uuid)),
    )

    await svc.recall("test query")

    assert scheduled == [
        ("entity-1", None),
        ("entity-2", None),
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recall_does_not_schedule_rehydration_for_active_results(
    stub_graphiti_client, stub_memory_graph_adapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_search_and_metadata(stub_graphiti_client, stub_memory_graph_adapter, freshness="ACTIVE")
    scheduled: list[tuple[str, str | None]] = []
    svc = _build_recall_service(
        stub_graphiti_client,
        stub_memory_graph_adapter,
        lifecycle_service=object(),
    )

    monkeypatch.setattr(
        svc,
        "_schedule_rehydration",
        lambda node_uuid, *, source_node_uuid=None: scheduled.append((node_uuid, source_node_uuid)),
    )

    await svc.recall("test query")

    assert scheduled == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fire_rehydration_calls_lifecycle_service(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    class FakeLifecycle:
        calls: list[dict[str, object]] = []

        async def rehydrate_node(self, node_uuid: str, new_context=None, *, source_node_uuid=None):
            self.calls.append(
                {
                    "node_uuid": node_uuid,
                    "new_context": new_context,
                    "source_node_uuid": source_node_uuid,
                }
            )
            return True

    lifecycle = FakeLifecycle()
    svc = _build_recall_service(
        stub_graphiti_client,
        stub_memory_graph_adapter,
        lifecycle_service=lifecycle,
    )

    await svc._fire_rehydration("entity-1", source_node_uuid="entity-1")

    assert lifecycle.calls == [
        {
            "node_uuid": "entity-1",
            "new_context": None,
            "source_node_uuid": "entity-1",
        }
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fire_rehydration_swallows_lifecycle_errors(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    class FailingLifecycle:
        async def rehydrate_node(self, node_uuid: str, new_context=None, *, source_node_uuid=None):
            raise RuntimeError("boom")

    svc = _build_recall_service(
        stub_graphiti_client,
        stub_memory_graph_adapter,
        lifecycle_service=FailingLifecycle(),
    )

    await svc._fire_rehydration("entity-1", source_node_uuid="entity-1")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recall_service_shutdown_cancels_pending_rehydration_tasks(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    svc = _build_recall_service(stub_graphiti_client, stub_memory_graph_adapter)

    started = asyncio.Event()

    async def _pending() -> None:
        started.set()
        await asyncio.sleep(60)

    task = asyncio.create_task(_pending())
    svc._track_rehydration_task(task)
    await started.wait()

    await svc.shutdown()

    assert task.cancelled()
    assert svc._rehydration_tasks == set()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recall_hybrid_path_uses_split_search(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    """enable_bm25 routes recall through the attributed split search, not search_scored."""
    stub_graphiti_client.ranked_by_method_results = {
        "cosine_similarity": [("vec-1", "Vector Hit")],
        "bm25": [("bm25-1", "Exact Match")],
    }
    stub_memory_graph_adapter.candidate_metadata = [
        _meta("vec-1", "Vector Hit"),
        _meta("bm25-1", "Exact Match"),
    ]
    svc = _build_recall_service(stub_graphiti_client, stub_memory_graph_adapter)

    result = await svc.recall("err string", tuning=RetrievalTuningConfig(enable_bm25=True))

    # Split search was used; the legacy fused path was not.
    assert len(stub_graphiti_client.ranked_by_method_calls) == 1
    assert stub_graphiti_client.search_scored_calls == []
    assert {r.uuid for r in result.results} == {"vec-1", "bm25-1"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recall_hybrid_bm25_only_hit_survives_floor(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    """A BM25-only candidate normalizes far below the 0.15 floor but survives by source.

    With alpha=0.99 the vector hit dominates fusion, so the bm25-only candidate's
    normalized similarity is ~0.01 (< MIN_SIMILARITY_THRESHOLD). Without the
    source-aware floor it would be dropped; as a BM25 source it must be kept.
    """
    stub_graphiti_client.ranked_by_method_results = {
        "cosine_similarity": [("vec-1", "Vector Hit")],
        "bm25": [("bm25-1", "Exact Match")],
    }
    stub_memory_graph_adapter.candidate_metadata = [
        _meta("vec-1", "Vector Hit"),
        _meta("bm25-1", "Exact Match"),
    ]
    svc = _build_recall_service(stub_graphiti_client, stub_memory_graph_adapter)

    result = await svc.recall(
        "err string", tuning=RetrievalTuningConfig(enable_bm25=True, hybrid_alpha=0.99)
    )

    uuids = {r.uuid for r in result.results}
    assert "bm25-1" in uuids  # exact lexical hit survives despite ~0.01 similarity
    # And its computed similarity really is below the floor (proves exemption, not luck).
    bm25_row = next(r for r in result.results if r.uuid == "bm25-1")
    assert bm25_row.breakdown.semantic_similarity < 0.15


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recall_default_path_unchanged(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    """Default tuning (enable_bm25=False) keeps the single fused search_scored path."""
    _setup_search_and_metadata(stub_graphiti_client, stub_memory_graph_adapter)
    svc = _build_recall_service(stub_graphiti_client, stub_memory_graph_adapter)

    await svc.recall("test query")

    assert len(stub_graphiti_client.search_scored_calls) == 1
    assert stub_graphiti_client.ranked_by_method_calls == []
    assert stub_graphiti_client.embed_query_calls == []
    assert stub_memory_graph_adapter.content_embedding_calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recall_content_vector_replace_name_uses_embedder_and_namespace(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    stub_graphiti_client.embed_query_result = [0.4, 0.6]
    stub_memory_graph_adapter.content_embedding_results = [
        {"uuid": "content-1", "name": "Content Hit", "cosine": 0.91}
    ]
    content_meta = _meta("content-1", "Content Hit")
    content_meta["namespace"] = "tenant-a"
    stub_memory_graph_adapter.candidate_metadata = [content_meta]
    svc = _build_recall_service(stub_graphiti_client, stub_memory_graph_adapter)

    result = await svc.recall(
        "paraphrased query",
        namespace="tenant-a",
        trace=True,
        tuning=RetrievalTuningConfig(
            enable_content_vector=True,
            content_vector_replace_name=True,
            fusion_admission_policy="production_fused",
        ),
    )

    assert [row.uuid for row in result.results] == ["content-1"]
    assert stub_graphiti_client.search_scored_calls == []
    assert stub_graphiti_client.ranked_by_method_calls == []
    assert stub_graphiti_client.embed_query_calls == ["paraphrased query"]
    assert stub_memory_graph_adapter.content_embedding_calls == [{
        "query_vector": [0.4, 0.6], "limit": 100, "group_ids": ["tenant-a"]
    }]
    traced = result.trace.candidates[0]
    assert traced.source is CandidateSource.VECTOR
    assert traced.contributing_sources == frozenset({CandidateSource.CONTENT_VECTOR})
    assert traced.content_rank == 1
    assert traced.content_cosine == pytest.approx(0.91)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recall_three_lane_fusion_preserves_provenance_without_bm25_exemption(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    stub_graphiti_client.ranked_by_method_results = {
        "cosine_similarity": [("shared", "Shared"), ("name", "Name")],
        "bm25": [("lexical", "Lexical"), ("shared", "Shared")],
    }
    stub_memory_graph_adapter.content_embedding_results = [
        {"uuid": "shared", "name": "Shared", "cosine": 0.95},
        {"uuid": "content", "name": "Content", "cosine": 0.90},
    ]
    stub_memory_graph_adapter.candidate_metadata = [
        _meta(uuid, name) for uuid, name in (
            ("shared", "Shared"), ("name", "Name"),
            ("lexical", "Lexical"), ("content", "Content"),
        )
    ]
    svc = _build_recall_service(stub_graphiti_client, stub_memory_graph_adapter)

    result = await svc.recall(
        "three lanes",
        trace=True,
        tuning=RetrievalTuningConfig(
            enable_bm25=True,
            enable_content_vector=True,
            fusion_admission_policy="production_fused",
        ),
    )

    traced = {row.uuid: row for row in result.trace.candidates}
    assert traced["shared"].contributing_sources == frozenset({
        CandidateSource.VECTOR, CandidateSource.BM25, CandidateSource.CONTENT_VECTOR
    })
    assert traced["lexical"].source is CandidateSource.VECTOR
    assert traced["shared"].content_rank == 1
    assert traced["shared"].content_cosine == pytest.approx(0.95)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_trace_shadows_method_ranks_without_changing_fused_order(
    stub_graphiti_client, stub_memory_graph_adapter, monkeypatch
) -> None:
    monkeypatch.setattr("menhir.services.recall_pipeline.days_ago", lambda _value: 1.0)
    _setup_search_and_metadata(stub_graphiti_client, stub_memory_graph_adapter)
    fused_order = [row[0] for row in stub_graphiti_client.search_scored_results]
    stub_graphiti_client.ranked_by_method_results = {
        "bm25": list(reversed([(uuid, name) for uuid, name, _score in stub_graphiti_client.search_scored_results])),
        "cosine_similarity": [(uuid, name) for uuid, name, _score in stub_graphiti_client.search_scored_results],
    }
    svc = _build_recall_service(stub_graphiti_client, stub_memory_graph_adapter)

    without_trace = await svc.recall("test query", trace=False)
    result = await svc.recall("test query", trace=True)

    assert [row.uuid for row in result.results] == fused_order[: len(result.results)]
    assert [row.uuid for row in result.results] == [row.uuid for row in without_trace.results]
    assert [row.final_score for row in result.results] == [
        row.final_score for row in without_trace.results
    ]
    assert result.trace is not None
    traced = {row.uuid: row for row in result.trace.candidates}
    assert traced[fused_order[0]].cosine_rank == 1
    assert traced[fused_order[0]].bm25_rank == len(fused_order)
    assert result.trace.rank_shadow_warning is None


# --- assertion shadow pass (observe-only oracle/warden) ---------------------

@pytest.mark.unit
@pytest.mark.asyncio
async def test_recall_no_trace_has_no_assertion_shadow(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    """Without trace=True the shadow pass never runs and no trace is attached."""
    _setup_search_and_metadata(stub_graphiti_client, stub_memory_graph_adapter)
    svc = _build_recall_service(stub_graphiti_client, stub_memory_graph_adapter)

    result = await svc.recall("test query")

    assert result.trace is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recall_trace_populates_assertion_shadow(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    """trace=True with the default flag attaches a shadow outcome covering every candidate."""
    _setup_search_and_metadata(stub_graphiti_client, stub_memory_graph_adapter)
    svc = _build_recall_service(stub_graphiti_client, stub_memory_graph_adapter)

    result = await svc.recall("test query", trace=True)

    shadow = result.trace.assertion_shadow
    assert shadow is not None
    # every candidate gets exactly one bucketed verdict
    assert len(shadow.rows) == 2
    assert shadow.admitted + shadow.flagged + shadow.refused == 2
    assert shadow.intent in {"current", "historical", "any"}
    assert {r.decision for r in shadow.rows} <= {"admit", "flag", "attenuate", "refuse"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recall_facet_shadow_off_by_default(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    """facet_shadow is default-off: a plain traced recall attaches no facet shadow."""
    _setup_search_and_metadata(stub_graphiti_client, stub_memory_graph_adapter)
    svc = _build_recall_service(stub_graphiti_client, stub_memory_graph_adapter)
    result = await svc.recall("test query", trace=True)
    assert result.trace.facet_shadow is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recall_facet_shadow_runs_when_enabled(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    """enable_facet_shadow attaches an observe-only facet shadow; a stub adapter with no
    Neo4j reader yields a graceful empty shadow (recall never breaks)."""
    _setup_search_and_metadata(stub_graphiti_client, stub_memory_graph_adapter)
    svc = _build_recall_service(stub_graphiti_client, stub_memory_graph_adapter)
    result = await svc.recall(
        "test query", trace=True, tuning=RetrievalTuningConfig(enable_facet_shadow=True)
    )
    shadow = result.trace.facet_shadow
    assert shadow is not None
    assert shadow.candidates == 0                 # no Neo4j reader on the stub adapter
    assert "no Neo4j reader" in shadow.note


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recall_active_facet_fuses_convergence_order_into_ranking(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    _setup_search_and_metadata(stub_graphiti_client, stub_memory_graph_adapter)

    class FacetNeo4j:
        def execute(self, query, params=None):
            assert set(params["uuids"]) == {"entity-1", "entity-2"}
            return [
                {
                    "uuid": "entity-1",
                    "content": "unrelated general memory",
                    "namespace": "default",
                    "conflict_status": None,
                    "created_at": None,
                    "files": [],
                    "projects": [],
                    "symbols": [],
                },
                {
                    "uuid": "entity-2",
                    "content": "fixed `api.py` and test_login",
                    "namespace": "default",
                    "conflict_status": None,
                    "created_at": None,
                    "files": [],
                    "projects": [],
                    "symbols": [],
                },
            ]

    stub_memory_graph_adapter.neo4j = FacetNeo4j()
    svc = _build_recall_service(stub_graphiti_client, stub_memory_graph_adapter)

    result = await svc.recall(
        "fix `api.py` test_login",
        trace=True,
        tuning=RetrievalTuningConfig(enable_facet_candidates=True, facet_weight=1.0),
    )

    assert [row.uuid for row in result.results] == ["entity-2", "entity-1"]
    assert "facet: active_rank_fusion" in (result.note or "")
    assert result.trace.facet_shadow is None
    assert result.trace.facet_active is not None
    assert [row.candidate_id for row in result.trace.facet_active.rows] == ["entity-2"]
    traced = {row.uuid: row for row in result.trace.candidates}
    assert CandidateSource.FACET in traced["entity-2"].contributing_sources


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recall_shadow_is_observe_only(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    """The shadow pass must not change which memories are returned or their order."""
    fixed = datetime(2026, 6, 1, tzinfo=timezone.utc)
    _setup_search_and_metadata(stub_graphiti_client, stub_memory_graph_adapter, last_accessed=fixed)
    svc = _build_recall_service(stub_graphiti_client, stub_memory_graph_adapter)
    without = await svc.recall("test query", trace=True,
                               tuning=RetrievalTuningConfig(enable_assertion_shadow=False))

    _setup_search_and_metadata(stub_graphiti_client, stub_memory_graph_adapter, last_accessed=fixed)
    with_shadow = await svc.recall("test query", trace=True)

    # Identical memories in identical order is the observe-only invariant. Scores are
    # compared with approx only to absorb ~1e-11 recency-vs-now() drift between the two
    # timed calls; the shadow pass runs after scoring and cannot move them.
    assert [r.uuid for r in with_shadow.results] == [r.uuid for r in without.results]
    assert [r.final_score for r in with_shadow.results] == pytest.approx(
        [r.final_score for r in without.results]
    )
    assert without.trace.assertion_shadow is None       # flag off -> no shadow
    assert with_shadow.trace.assertion_shadow is not None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recall_shadow_failure_never_breaks_recall(
    stub_graphiti_client, stub_memory_graph_adapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash inside the shadow pass is swallowed; recall results still return."""
    _setup_search_and_metadata(stub_graphiti_client, stub_memory_graph_adapter)
    svc = _build_recall_service(stub_graphiti_client, stub_memory_graph_adapter)

    async def _boom(*a, **k):
        raise RuntimeError("shadow boom")

    monkeypatch.setattr(svc, "_run_assertion_shadow", _boom)

    result = await svc.recall("test query", trace=True)

    assert len(result.results) == 2                      # recall unharmed
    assert result.trace.assertion_shadow is None         # shadow absent, not fatal


# --- frontier portions (active oracle ranking / warden gate) ----------------

def _two_cands(stub_graphiti_client, stub_memory_graph_adapter, *, c1, c2, extra2=None):
    """Two PERSISTENT/ACTIVE candidates; entity-1 has the higher search similarity."""
    stub_graphiti_client.search_scored_results = [
        ("entity-1", "Entity One", 0.85),
        ("entity-2", "Entity Two", 0.65),
    ]
    def _row(uuid: str, name: str, content: str) -> dict:
        return {
            "uuid": uuid, "name": name, "scope": "PERSISTENT", "type": "SEMANTIC",
            "content": content, "summary": None, "last_accessed": datetime.now(timezone.utc),
            "edge_count": 1, "sharpness": 0.2, "freshness": "ACTIVE", "user_flagged": False,
        }
    m1 = _row("entity-1", "Entity One", c1)
    m2 = _row("entity-2", "Entity Two", c2)
    if extra2:
        m2.update(extra2)
    stub_memory_graph_adapter.candidate_metadata = [m1, m2]
    # Derived evidence: a git Evidence node on each, so EvidenceAnchorWarden admits
    # (it REFUSES unanchored claims). This is the provenance _attach_frontier_metadata reads.
    stub_memory_graph_adapter.candidate_provenance_rows = [
        {"uuid": "entity-1", "evidence_node_kinds": ["git"],
         "anchor_projects": [], "episode_sources": []},
        {"uuid": "entity-2", "evidence_node_kinds": ["git"],
         "anchor_projects": [], "episode_sources": []},
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recall_frontier_off_by_default_is_old_order(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    """Default tuning runs no frontier portion: similarity order, no label, no note."""
    _setup_search_and_metadata(stub_graphiti_client, stub_memory_graph_adapter)
    svc = _build_recall_service(stub_graphiti_client, stub_memory_graph_adapter)

    result = await svc.recall("test query")

    assert [r.uuid for r in result.results] == ["entity-1", "entity-2"]
    assert all(r.warden_label is None for r in result.results)
    assert "frontier" not in (result.note or "")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recall_oracle_ranking_is_bounded_residual_not_replacement(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    """Evidence strength cannot replace query relevance or erase the upstream winner."""
    _two_cands(stub_graphiti_client, stub_memory_graph_adapter, c1="alpha", c2="beta")
    stub_memory_graph_adapter.candidate_provenance_rows = [
        {"uuid": "entity-1", "evidence_node_kinds": [],
         "anchor_projects": [], "episode_sources": ["claude-code"]},        # weak: agent only
        {"uuid": "entity-2", "evidence_node_kinds": ["git", "test"],
         "anchor_projects": [], "episode_sources": ["user"]},                # strong anchor
    ]
    svc = _build_recall_service(stub_graphiti_client, stub_memory_graph_adapter)

    base = await svc.recall("alpha beta")
    assert [r.uuid for r in base.results] == ["entity-1", "entity-2"]   # shipped: by similarity

    flip = await svc.recall("alpha beta",
                            tuning=RetrievalTuningConfig(enable_oracle_ranking=True))
    assert [r.uuid for r in flip.results] == ["entity-1", "entity-2"]
    assert "oracle_ranking" in (flip.note or "")


@pytest.mark.unit
def test_oracle_similarity_normalizes_rrf_without_rescaling_source_prior() -> None:
    assert _oracle_similarity(2.0, RetrievalScoreKind.GRAPHITI_RRF) == 1.0
    assert _oracle_similarity(1.4, RetrievalScoreKind.WEIGHTED_RRF_NORMALIZED) == 0.7
    assert _oracle_similarity(0.8, RetrievalScoreKind.SOURCE_PRIOR) == 0.8


@pytest.mark.unit
def test_oracle_residual_preserves_head_and_can_adjust_close_tail() -> None:
    scored = [SimpleNamespace(uuid=uuid) for uuid in ("a", "b", "c", "d")]
    oracle_rank = {"d": 0, "c": 1, "b": 2, "a": 3}

    blended = _blend_oracle_order(scored, oracle_rank, oracle_weight=0.25)

    assert [row.uuid for row in blended] == ["a", "b", "d", "c"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recall_warden_gate_drops_superseded(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    """ON: a superseded (expired_at) candidate is REFUSED under the default current lens."""
    _two_cands(stub_graphiti_client, stub_memory_graph_adapter,
               c1="alpha fact", c2="beta fact",
               extra2={"expired_at": "2026-02-01", "created_at": "2026-01-01"})
    svc = _build_recall_service(stub_graphiti_client, stub_memory_graph_adapter)

    base = await svc.recall("alpha beta")
    assert {r.uuid for r in base.results} == {"entity-1", "entity-2"}   # off: both kept

    gated = await svc.recall("alpha beta",
                             tuning=RetrievalTuningConfig(enable_warden_gate=True))
    ids = {r.uuid for r in gated.results}
    assert "entity-2" not in ids        # superseded refused from current-truth context
    assert "entity-1" in ids
    assert "warden_gate" in (gated.note or "")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recall_warden_gate_refuses_agent_only_provenance(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    """A candidate whose only provenance is an agent-harness episode source (claude-code ->
    agent_inference) has no external anchor, so EvidenceAnchorWarden refuses it (rule zero)."""
    _setup_search_and_metadata(stub_graphiti_client, stub_memory_graph_adapter)
    stub_memory_graph_adapter.candidate_provenance_rows = [
        {"uuid": "entity-1", "evidence_node_kinds": [],
         "anchor_projects": [], "episode_sources": ["claude-code"]},
        {"uuid": "entity-2", "evidence_node_kinds": [],
         "anchor_projects": [], "episode_sources": ["claude-code"]},
    ]
    svc = _build_recall_service(stub_graphiti_client, stub_memory_graph_adapter)

    gated = await svc.recall("test query",
                             tuning=RetrievalTuningConfig(enable_warden_gate=True))

    assert gated.results == []          # agent-only provenance -> no external anchor -> refused


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recall_warden_gate_admits_structural_anchor(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    """A structural ANCHORED_TO link derives a `file` anchor, which admits the candidate even
    with no Evidence node and an agent-only episode source."""
    _setup_search_and_metadata(stub_graphiti_client, stub_memory_graph_adapter)
    stub_memory_graph_adapter.candidate_provenance_rows = [
        {"uuid": "entity-1", "evidence_node_kinds": [],
         "anchor_projects": ["menhir"], "episode_sources": ["claude-code"]},
        {"uuid": "entity-2", "evidence_node_kinds": [],
         "anchor_projects": [], "episode_sources": ["user"]},
    ]
    svc = _build_recall_service(stub_graphiti_client, stub_memory_graph_adapter)

    gated = await svc.recall("test query",
                             tuning=RetrievalTuningConfig(enable_warden_gate=True))

    ids = {r.uuid for r in gated.results}
    assert ids == {"entity-1", "entity-2"}   # file anchor + user source both admit


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recall_warden_gate_refuses_cross_project_scope(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    """ScopeOracle project axis: a candidate anchored to project B is REFUSED for a query
    scoped to project A (file_context_project), while the same-project one is admitted."""
    _two_cands(stub_graphiti_client, stub_memory_graph_adapter, c1="alpha", c2="beta")
    # entity-1 is in project A (query's project); entity-2 is in project B (wrong scope).
    stub_memory_graph_adapter.candidate_provenance_rows = [
        {"uuid": "entity-1", "evidence_node_kinds": ["git"],
         "anchor_projects": ["proj-a"], "episode_sources": []},
        {"uuid": "entity-2", "evidence_node_kinds": ["git"],
         "anchor_projects": ["proj-b"], "episode_sources": []},
    ]
    svc = _build_recall_service(stub_graphiti_client, stub_memory_graph_adapter)

    gated = await svc.recall("alpha beta", file_context_project="proj-a",
                             tuning=RetrievalTuningConfig(enable_warden_gate=True))

    ids = {r.uuid for r in gated.results}
    assert "entity-2" not in ids        # wrong-project memory refused
    assert "entity-1" in ids            # same-project memory admitted


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recall_scope_permissive_without_query_project(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    """No query project (no file_context_project) -> ScopeOracle stays permissive: a
    cross-project candidate is NOT refused on the scope axis (absence is unknown, not non-self)."""
    _two_cands(stub_graphiti_client, stub_memory_graph_adapter, c1="alpha", c2="beta")
    stub_memory_graph_adapter.candidate_provenance_rows = [
        {"uuid": "entity-1", "evidence_node_kinds": ["git"],
         "anchor_projects": ["proj-a"], "episode_sources": []},
        {"uuid": "entity-2", "evidence_node_kinds": ["git"],
         "anchor_projects": ["proj-b"], "episode_sources": []},
    ]
    svc = _build_recall_service(stub_graphiti_client, stub_memory_graph_adapter)

    gated = await svc.recall("alpha beta",   # no file_context_project
                             tuning=RetrievalTuningConfig(enable_warden_gate=True))

    ids = {r.uuid for r in gated.results}
    assert ids == {"entity-1", "entity-2"}   # both kept: scope unknown on the query side


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recall_multi_project_anchor_left_unscoped(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    """A memory anchored to MULTIPLE projects is left unscoped (no project), so it is not
    refused as cross-project -- avoids inventing a false conflict."""
    _two_cands(stub_graphiti_client, stub_memory_graph_adapter, c1="alpha", c2="beta")
    stub_memory_graph_adapter.candidate_provenance_rows = [
        {"uuid": "entity-1", "evidence_node_kinds": ["git"],
         "anchor_projects": ["proj-a", "proj-b"], "episode_sources": []},  # spans projects
        {"uuid": "entity-2", "evidence_node_kinds": ["git"],
         "anchor_projects": ["proj-a"], "episode_sources": []},
    ]
    svc = _build_recall_service(stub_graphiti_client, stub_memory_graph_adapter)

    gated = await svc.recall("alpha beta", file_context_project="proj-a",
                             tuning=RetrievalTuningConfig(enable_warden_gate=True))

    ids = {r.uuid for r in gated.results}
    assert ids == {"entity-1", "entity-2"}   # multi-project memory not refused (left unscoped)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recall_belief_gate_flag_defaults_off(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    """enable_belief_gate defaults False; when off, frontier_active does not include it."""
    assert RetrievalTuningConfig().enable_belief_gate is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recall_belief_gate_without_warden_gate_is_inert_and_warns(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    """belief_gate adds CurrentnessWarden but does NOT apply verdicts without warden_gate.

    A superseded candidate stays in results (verdict computed, not applied), and recall emits
    a 'belief_gate has no effect without warden_gate' note so the gate is not silently inert."""
    _two_cands(stub_graphiti_client, stub_memory_graph_adapter, c1="current fact", c2="old fact")
    stub_memory_graph_adapter.temporal_fact_rows = [
        {"node_uuid": "entity-1", "expired_at": None},
        {"node_uuid": "entity-2", "expired_at": "2025-01-02"},   # superseded belief
    ]
    svc = _build_recall_service(stub_graphiti_client, stub_memory_graph_adapter)

    gated = await svc.recall("test", tuning=RetrievalTuningConfig(enable_belief_gate=True))

    # inert: superseded candidate is NOT dropped because warden_gate did not apply the chain
    assert "entity-2" in {r.uuid for r in gated.results}
    assert "belief_gate has no effect without warden_gate" in (gated.note or "")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recall_belief_gate_with_warden_gate_drops_superseded_belief(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    """With BOTH belief_gate and warden_gate, CurrentnessWarden's verdict is applied.

    entity-2 is superseded only via belief markers (temporal facts), which warden_gate alone
    does not fetch or see -- so the gating is the belief gate's marginal contribution."""
    _two_cands(stub_graphiti_client, stub_memory_graph_adapter, c1="current fact", c2="old fact")
    stub_memory_graph_adapter.temporal_fact_rows = [
        {"node_uuid": "entity-1", "expired_at": None},
        {"node_uuid": "entity-2", "expired_at": "2025-01-02"},   # superseded belief
    ]
    svc = _build_recall_service(stub_graphiti_client, stub_memory_graph_adapter)

    # warden_gate alone: belief markers are not fetched, supersession invisible -> entity-2 kept
    warden_only = await svc.recall("test", tuning=RetrievalTuningConfig(enable_warden_gate=True))
    assert "entity-2" in {r.uuid for r in warden_only.results}

    # belief_gate + warden_gate: CurrentnessWarden runs AND the chain is applied -> entity-2 gated
    both = await svc.recall("test", tuning=RetrievalTuningConfig(
        enable_warden_gate=True, enable_belief_gate=True))
    e2 = [r for r in both.results if r.uuid == "entity-2"]
    assert (not e2) or (e2[0].warden_label is not None)   # refused (dropped) or flagged
    assert "belief_gate" in (both.note or "") and "warden_gate" in (both.note or "")
    assert "has no effect" not in (both.note or "")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recall_belief_gate_temporal_fetch_failure_degrades(
    stub_graphiti_client, stub_memory_graph_adapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Belief marker fetch is best-effort: DB failure treats candidates as untimed."""
    _setup_search_and_metadata(stub_graphiti_client, stub_memory_graph_adapter)
    svc = _build_recall_service(stub_graphiti_client, stub_memory_graph_adapter)

    def _boom(node_uuids: list[str]) -> list[dict[str, object]]:
        raise RuntimeError("temporal fetch unavailable")

    monkeypatch.setattr(stub_memory_graph_adapter, "fetch_temporal_facts", _boom)

    result = await svc.recall("test query", tuning=RetrievalTuningConfig(enable_belief_gate=True))

    assert [r.uuid for r in result.results] == ["entity-1", "entity-2"]
    assert "belief_gate" in (result.note or "")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recall_frontier_failure_degrades_to_old_order(
    stub_graphiti_client, stub_memory_graph_adapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash in the active frontier pass degrades to the ScoringService order, not a 500."""
    _setup_search_and_metadata(stub_graphiti_client, stub_memory_graph_adapter)
    svc = _build_recall_service(stub_graphiti_client, stub_memory_graph_adapter)

    import menhir.services.assertion_pipeline as ap

    async def _boom(self, query, candidates):
        raise RuntimeError("frontier boom")

    monkeypatch.setattr(ap.AssertionPipeline, "run", _boom)

    result = await svc.recall("test query",
                              tuning=RetrievalTuningConfig(enable_oracle_ranking=True))

    assert [r.uuid for r in result.results] == ["entity-1", "entity-2"]  # degraded to old order
    assert "frontier" not in (result.note or "")


@pytest.mark.unit
def test_compact_scored_item_surfaces_warden_label() -> None:
    """The MCP formatter exposes a warden FLAG label (kept even in compact mode)."""
    from types import SimpleNamespace

    from menhir.mcp.formatters import _compact_scored_item

    item = _compact_scored_item(
        SimpleNamespace(uuid="x", name="n", scope="PERSISTENT", final_score=0.5,
                        breakdown=None, content="c", warden_label="historical"),
        compact=True,
    )
    assert item["warden_label"] == "historical"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recall_diversity_gate_on_path_is_wired(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    """ON-path: diversity gate executes on collapsed sets and reorders minority family up.

    Setup: three candidates with evidence families:
    - entity-1, entity-2: evidence_node_kinds [user, log] → "user_log_test" family (dominant, 2/3)
    - entity-3: evidence_node_kinds [git] → "git_temporal" family (minority, 1/3)
    This triggers is_collapsed() (dominance 2/3 > 0.6). With gate OFF, order is scored
    [entity-1, entity-2, entity-3]. With gate ON, diversify interleaves families, pulling
    minority-family entity-3 earlier in the output."""
    stub_graphiti_client.search_scored_results = [
        ("entity-1", "Entity One", 0.85),
        ("entity-2", "Entity Two", 0.75),
        ("entity-3", "Entity Three", 0.50),  # lowest score: ranks last when gate OFF
    ]
    stub_memory_graph_adapter.candidate_metadata = [
        {
            "uuid": "entity-1", "name": "Entity One", "scope": "PERSISTENT", "type": "SEMANTIC",
            "content": "alpha", "summary": None, "last_accessed": datetime.now(timezone.utc),
            "edge_count": 1, "sharpness": 0.2, "freshness": "ACTIVE", "user_flagged": False,
        },
        {
            "uuid": "entity-2", "name": "Entity Two", "scope": "PERSISTENT", "type": "SEMANTIC",
            "content": "beta", "summary": None, "last_accessed": datetime.now(timezone.utc),
            "edge_count": 1, "sharpness": 0.2, "freshness": "ACTIVE", "user_flagged": False,
        },
        {
            "uuid": "entity-3", "name": "Entity Three", "scope": "PERSISTENT", "type": "SEMANTIC",
            "content": "gamma", "summary": None, "last_accessed": datetime.now(timezone.utc),
            "edge_count": 1, "sharpness": 0.2, "freshness": "ACTIVE", "user_flagged": False,
        },
    ]
    stub_memory_graph_adapter.candidate_provenance_rows = [
        {"uuid": "entity-1", "evidence_node_kinds": ["user"],
         "anchor_projects": [], "episode_sources": []},
        {"uuid": "entity-2", "evidence_node_kinds": ["log"],
         "anchor_projects": [], "episode_sources": []},
        {"uuid": "entity-3", "evidence_node_kinds": ["git"],
         "anchor_projects": [], "episode_sources": []},
    ]
    svc = _build_recall_service(stub_graphiti_client, stub_memory_graph_adapter)

    # Gate OFF: returns scored order (entity-3 last due to lowest score).
    off = await svc.recall("query")
    off_uuids = [r.uuid for r in off.results]
    assert off_uuids == ["entity-1", "entity-2", "entity-3"]

    # Gate ON: diversity gate reorders to interleave families, pulling minority-family entity-3
    # earlier than it was in the scored order.
    gated = await svc.recall("query",
                             tuning=RetrievalTuningConfig(
                                 enable_warden_gate=True, enable_diversity_gate=True))
    gated_uuids = [r.uuid for r in gated.results]
    # Same set, but reordered.
    assert sorted(gated_uuids) == sorted(off_uuids)
    # Minority family promoted: entity-3's position moved earlier.
    assert gated_uuids.index("entity-3") < off_uuids.index("entity-3")
    # Gate executed.
    assert "diversity_gate" in (gated.note or "")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recall_belief_gate_with_git_staleness_evidence(
    stub_graphiti_client, stub_memory_graph_adapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Git staleness evidence (LATER_CONTRADICTED) gates entity-2 when both belief_gate and
    warden_gate are on. entity-2 has belief_commit="C" + created_at set, and its provenance
    includes anchor_paths=["a.py"] + anchor_projects=["proj"]. A fake ChangeLogProvider
    reports a change to a.py after commit C, yielding LATER_CONTRADICTED. With both gates on,
    entity-2 is gated (dropped or flagged); with gates off, both results returned."""
    _two_cands(stub_graphiti_client, stub_memory_graph_adapter, c1="alpha", c2="beta")
    now = datetime.now(timezone.utc)
    stub_memory_graph_adapter.candidate_metadata = [
        {
            "uuid": "entity-1", "name": "Entity One", "scope": "PERSISTENT", "type": "SEMANTIC",
            "content": "alpha", "summary": None, "last_accessed": now,
            "edge_count": 1, "sharpness": 0.2, "freshness": "ACTIVE", "user_flagged": False,
        },
        {
            "uuid": "entity-2", "name": "Entity Two", "scope": "PERSISTENT", "type": "SEMANTIC",
            "content": "beta", "summary": None, "last_accessed": now,
            "edge_count": 1, "sharpness": 0.2, "freshness": "ACTIVE", "user_flagged": False,
            "belief_commit": "C", "created_at": now.isoformat(),
        },
    ]
    stub_memory_graph_adapter.candidate_provenance_rows = [
        {"uuid": "entity-1", "evidence_node_kinds": ["git"],
         "anchor_paths": [], "anchor_projects": [], "episode_sources": []},
        {"uuid": "entity-2", "evidence_node_kinds": ["git"],
         "anchor_paths": ["a.py"], "anchor_projects": ["proj"], "episode_sources": []},
    ]

    # Fake git change log: a.py changed after commit C (git-stale).
    from menhir.domain.git_staleness import GitChange
    class FakeChangeLogProvider:
        def changes(self, repo_path: str, *, since_commit: str | None, paths: list[str]):
            # Simulate a change to a.py after commit C (descends from C)
            if "a.py" in paths and since_commit == "C":
                return [GitChange(
                    target="a.py", changed_at="2026-02-01T00:00:00Z", kind="file",
                    commit="D", descends_from=frozenset({"C"})
                )]
            return []

    svc = _build_recall_service(stub_graphiti_client, stub_memory_graph_adapter)
    svc._change_log_provider = FakeChangeLogProvider()

    # Monkeypatch _repo_path_for to return a dummy path so no real git is invoked.
    import menhir.services.recall_pipeline as rs
    monkeypatch.setattr(rs, "_repo_path_for", lambda project: "/repo" if project == "proj" else None)

    # With gates off: both candidates returned in search order.
    off_result = await svc.recall("alpha beta")
    off_uuids = [r.uuid for r in off_result.results]
    assert set(off_uuids) == {"entity-1", "entity-2"}

    # With gates on: entity-2 (git-stale) is gated out or flagged.
    gated_result = await svc.recall(
        "alpha beta",
        tuning=RetrievalTuningConfig(enable_belief_gate=True, enable_warden_gate=True)
    )
    gated_uuids = [r.uuid for r in gated_result.results]
    e2_rows = [r for r in gated_result.results if r.uuid == "entity-2"]
    # Either dropped (not in results) or flagged (has warden_label).
    if e2_rows:
        assert e2_rows[0].warden_label is not None
    else:
        assert "entity-2" not in gated_uuids
    assert "belief_gate" in (gated_result.note or "")
    assert "warden_gate" in (gated_result.note or "")


def _one_observation_hit():
    return [{
        "assertion_id": "obs-aid-1", "stated_span": "I own 20 rare coins", "cosine": 0.99,
        "subject_uuid": "ent-coins", "subject_display": "user", "attribute": "owned", "scope": "",
        "value_kind": "count", "unit": "", "namespace": "proj", "evidence_tier": "agent",
        "operation": "absolute", "value": "20", "valid_at": "2026-07-01T00:00:00+00:00",
    }]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_observation_lane_injects_candidate_when_flag_on(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    # Phase 4a.2: behind scalar_view_authority_enabled, a :TypedAssertion observation surfaces as a
    # recall candidate even though the pipeline is otherwise :Entity-only.
    _setup_search_and_metadata(stub_graphiti_client, stub_memory_graph_adapter)
    calls: list[dict] = []
    stub_memory_graph_adapter.search_assertion_embeddings = (
        lambda vec, *, limit, namespaces: calls.append({"namespaces": namespaces, "limit": limit})
        or _one_observation_hit()
    )
    svc = _build_recall_service(
        stub_graphiti_client, stub_memory_graph_adapter, scalar_view_authority_enabled=True)

    res = await svc.recall("how many rare coins", namespace="proj")

    assert "I own 20 rare coins" in [r.name for r in res.results]   # the observation was injected
    assert len(calls) == 1 and calls[0]["namespaces"] == ["proj"]   # lane searched, namespace-scoped


@pytest.mark.unit
@pytest.mark.asyncio
async def test_enforce_mode_excludes_legacy_canonical_self_node(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    namespace = "proj"
    canonical_uuid = self_uuid_for_namespace(namespace)
    stub_graphiti_client.search_scored_results = [
        (canonical_uuid, "user", 0.99),
    ]
    canonical_meta = _meta(canonical_uuid, "user")
    canonical_meta["namespace"] = namespace
    stub_memory_graph_adapter.candidate_metadata = [canonical_meta]
    stub_memory_graph_adapter.canonical_self_binding_mode = "enforce"
    svc = _build_recall_service(stub_graphiti_client, stub_memory_graph_adapter)

    result = await svc.recall("what do I know about myself", namespace=namespace)

    assert result.results == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_enforce_mode_excludes_legacy_canonical_self_view(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    namespace = "proj"
    canonical_uuid = self_uuid_for_namespace(namespace)
    stub_graphiti_client.search_scored_results = [("view-1", "legacy self view", 0.99)]
    view_meta = _meta("view-1", "legacy self view")
    view_meta["namespace"] = namespace
    view_meta["view_subject_uuid"] = canonical_uuid
    stub_memory_graph_adapter.candidate_metadata = [view_meta]
    stub_memory_graph_adapter.canonical_self_binding_mode = "enforce"
    svc = _build_recall_service(stub_graphiti_client, stub_memory_graph_adapter)

    result = await svc.recall("what do I know about myself", namespace=namespace)

    assert result.results == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_enforce_mode_excludes_unscoped_canonical_self_view(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    namespace = "tenant-a"
    stub_graphiti_client.search_scored_results = [("view-1", "legacy self view", 0.99)]
    view_meta = _meta("view-1", "legacy self view")
    view_meta["namespace"] = namespace
    view_meta["view_subject_uuid"] = self_uuid_for_namespace(namespace)
    stub_memory_graph_adapter.candidate_metadata = [view_meta]
    stub_memory_graph_adapter.canonical_self_binding_mode = "enforce"
    svc = _build_recall_service(stub_graphiti_client, stub_memory_graph_adapter)

    result = await svc.recall("what do I know about myself")

    assert result.results == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_enforce_mode_excludes_structurally_marked_self_even_with_wrong_uuid(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    stub_graphiti_client.search_scored_results = [("legacy-wrong-uuid", "user", 0.99)]
    meta = _meta("legacy-wrong-uuid", "user")
    meta["namespace"] = "tenant-a"
    meta["is_self"] = True
    stub_memory_graph_adapter.candidate_metadata = [meta]
    stub_memory_graph_adapter.canonical_self_binding_mode = "enforce"
    svc = _build_recall_service(stub_graphiti_client, stub_memory_graph_adapter)

    result = await svc.recall("what do I know about myself")

    assert result.results == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_enforce_mode_withholds_unconfirmed_self_fact_from_counterpart_summary(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    namespace = "tenant-a"
    city_uuid = "city-1"
    stub_graphiti_client.search_scored_results = [(city_uuid, "Chicago", 0.99)]
    meta = _meta(city_uuid, "Chicago")
    meta["namespace"] = namespace
    meta["content"] = "Chicago is where the user lives"
    stub_memory_graph_adapter.candidate_metadata = [meta]
    stub_memory_graph_adapter.temporal_fact_rows = [{
        "node_uuid": city_uuid,
        "other_node_uuid": self_uuid_for_namespace(namespace),
        "edge_source_uuid": self_uuid_for_namespace(namespace),
        "edge_target_uuid": city_uuid,
        "edge_source_name": "user",
        "edge_target_name": "Chicago",
        "fact_uuid": "legacy-self-edge",
        "group_id": namespace,
        "self_authority_payload_json": None,
        "predicate": "LIVES_IN",
        "fact": "user lives in Chicago",
        "valid_at": None,
        "invalid_at": None,
        "created_at": None,
        "expired_at": None,
    }]
    stub_graphiti_client.canonical_self_payload_is_authorized = (
        lambda payload, **kwargs: False
    )
    stub_memory_graph_adapter.canonical_self_binding_mode = "enforce"
    svc = _build_recall_service(stub_graphiti_client, stub_memory_graph_adapter)

    result = await svc.recall("Chicago", namespace=namespace)

    assert [row.name for row in result.results] == ["Chicago"]
    assert result.results[0].content == "Chicago"
    assert result.results[0].temporal_facts == ()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_enforce_mode_excludes_legacy_canonical_self_scalar_observation(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    namespace = "proj"
    _setup_search_and_metadata(stub_graphiti_client, stub_memory_graph_adapter)
    hit = dict(_one_observation_hit()[0])
    hit["subject_uuid"] = self_uuid_for_namespace(namespace)
    stub_memory_graph_adapter.search_assertion_embeddings = (
        lambda vec, *, limit, namespaces: [hit]
    )
    view_calls: list[dict] = []
    stub_memory_graph_adapter.fetch_current_scalar_view_for_slot = (
        lambda **kwargs: view_calls.append(kwargs) or None
    )
    stub_memory_graph_adapter.canonical_self_binding_mode = "enforce"
    svc = _build_recall_service(
        stub_graphiti_client,
        stub_memory_graph_adapter,
        scalar_view_authority_enabled=True,
    )

    result = await svc.recall("how many rare coins", namespace=namespace)

    assert "I own 20 rare coins" not in [row.name for row in result.results]
    assert view_calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_observation_lane_not_consulted_when_flag_off(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    # Flag OFF (default): the observation lane is never searched -> recall is byte-identical.
    _setup_search_and_metadata(stub_graphiti_client, stub_memory_graph_adapter)
    calls: list[int] = []
    stub_memory_graph_adapter.search_assertion_embeddings = (
        lambda vec, *, limit, namespaces: calls.append(1) or _one_observation_hit()
    )
    svc = _build_recall_service(stub_graphiti_client, stub_memory_graph_adapter)

    res = await svc.recall("how many rare coins", namespace="proj")

    assert calls == []                                              # lane not consulted
    assert "I own 20 rare coins" not in [r.name for r in res.results]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_scalar_authority_view_injected_for_surfaced_slot(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    # Phase 4a.4 (the payoff / G5 fix): a STALE observation ("20") embedding-matches the query, but the
    # authoritative CURRENT View for that slot ("37") is injected DETERMINISTICALLY by slot-keyed lookup
    # -- so "how many rare coins now" surfaces 37 even though the View itself never embedding-ranked.
    _setup_search_and_metadata(stub_graphiti_client, stub_memory_graph_adapter)
    stub_memory_graph_adapter.search_assertion_embeddings = (
        lambda vec, *, limit, namespaces: _one_observation_hit()  # the stale "20" observation
    )
    view_calls: list[dict] = []

    def _fetch_view(*, subject_uuid, attribute, scope, value_kind, unit, namespace):
        view_calls.append({"subject_uuid": subject_uuid, "attribute": attribute})
        return {
            "uuid": "view-owned-current", "value": 37,
            "name": "user's owned: 37. current owned = 37.", "view_key": "vk-owned",
            "attribute": "owned", "subject_uuid": subject_uuid, "namespace": "proj",
            "valid_at": "2026-07-02T00:00:00+00:00",
            "recall_eligible": True,
        }

    stub_memory_graph_adapter.fetch_current_scalar_view_for_slot = _fetch_view
    # Phase 4b foundation gate: the slot's effective tier (from the fold SSOT) is a FOUNDATION tier
    # ("user"), so the injected View may LEAD as the current authority.
    stub_memory_graph_adapter.scalar_state_service = lambda: SimpleNamespace(
        current_authority=lambda subj, *, namespace, as_of: {("owned", "", "count", ""): "user"})
    svc = _build_recall_service(
        stub_graphiti_client, stub_memory_graph_adapter, scalar_view_authority_enabled=True)

    res = await svc.recall("how many rare coins now", namespace="proj")

    by_name = {r.name: r for r in res.results}
    assert "user's owned: 37. current owned = 37." in by_name        # current authority injected
    assert "I own 20 rare coins" in by_name                          # stale observation still present
    # the authority is unambiguously MARKED (Phase 4c), the observation is not.
    assert by_name["user's owned: 37. current owned = 37."].is_scalar_authority is True
    assert by_name["I own 20 rare coins"].is_scalar_authority is False
    assert res.authority_layer is not None and len(res.authority_layer) == 1
    verdict = res.authority_layer[0]
    assert verdict.kind == "current" and verdict.status == "leads" and verdict.value == 37
    assert verdict.contributors[0].relation == "CURRENT_ANCHOR"
    assert verdict.contributors[0].stated_span == "I own 37 rare coins"
    # the View was resolved by the surfaced observation's SLOT (owned), not by ranking.
    assert view_calls and view_calls[0]["attribute"] == "owned"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_asof_query_with_unresolvable_date_injects_nothing(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    # Regression: "as of 2026-02-30" matches the as-of cue regex (\d{4}-\d{2}-\d{2}), so the intent
    # is COMPARISON, but strptime rejects the impossible calendar date -> _as_of_dt is None. The
    # as-of branch was guarded on `_as_of_dt is not None`, so this fell THROUGH to the current-View
    # injection and answered a historical question with today's value (and the expiry verdict below
    # is current-state-only, so nothing corrected it). An unresolvable as-of must inject nothing.
    _setup_search_and_metadata(stub_graphiti_client, stub_memory_graph_adapter)
    stub_memory_graph_adapter.search_assertion_embeddings = (
        lambda vec, *, limit, namespaces: _one_observation_hit()
    )
    view_calls: list[dict] = []

    def _fetch_view(*, subject_uuid, attribute, scope, value_kind, unit, namespace):
        view_calls.append({"subject_uuid": subject_uuid, "attribute": attribute})
        return {
            "uuid": "view-owned-current", "value": 37,
            "name": "user's owned: 37. current owned = 37.", "view_key": "vk-owned",
            "attribute": "owned", "subject_uuid": subject_uuid, "namespace": "proj",
            "valid_at": "2026-07-02T00:00:00+00:00",
            "recall_eligible": True,
        }

    stub_memory_graph_adapter.fetch_current_scalar_view_for_slot = _fetch_view
    stub_memory_graph_adapter.scalar_state_service = lambda: SimpleNamespace(
        current_authority=lambda subj, *, namespace, as_of: {("owned", "", "count", ""): "user"})
    svc = _build_recall_service(
        stub_graphiti_client, stub_memory_graph_adapter, scalar_view_authority_enabled=True)

    res = await svc.recall("how many rare coins as of 2026-02-30", namespace="proj")

    # the current-View lookup must never be reached for an as-of query
    assert view_calls == []
    assert not res.authority_layer
    by_name = {r.name: r for r in res.results}
    assert "user's owned: 37. current owned = 37." not in by_name  # current value NOT presented
    assert not any(r.is_scalar_authority for r in res.results)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_scalar_authority_annotates_view_that_already_won_vector_search(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    _setup_search_and_metadata(stub_graphiti_client, stub_memory_graph_adapter)
    view_name = "user's owned: 37. current owned = 37."
    stub_graphiti_client.search_scored_results.append(("view-owned-current", view_name, 0.9))
    stub_memory_graph_adapter.candidate_metadata.append({
        **_meta("view-owned-current", view_name),
        "content": view_name,
        "view_kind": "scalar_state",
        "view_current": True,
        "namespace": "proj",
    })
    stub_memory_graph_adapter.search_assertion_embeddings = (
        lambda vec, *, limit, namespaces: _one_observation_hit())
    stub_memory_graph_adapter.fetch_current_scalar_view_for_slot = (
        lambda *, subject_uuid, attribute, scope, value_kind, unit, namespace: {
            "uuid": "view-owned-current", "value": 37, "name": view_name,
            "attribute": "owned", "subject_uuid": subject_uuid, "namespace": "proj",
            "valid_at": "2026-07-02T00:00:00+00:00",
            "recall_eligible": True,
        })
    stub_memory_graph_adapter.scalar_state_service = lambda: SimpleNamespace(
        current_authority=lambda subj, *, namespace, as_of: {
            ("owned", "", "count", ""): "user"})
    svc = _build_recall_service(
        stub_graphiti_client, stub_memory_graph_adapter, scalar_view_authority_enabled=True)

    res = await svc.recall("how many rare coins now", namespace="proj")

    matched = [r for r in res.results if r.uuid == "view-owned-current"]
    assert len(matched) == 1
    assert matched[0].is_scalar_authority is True
    assert res.authority_layer is not None
    assert [(v.attribute, v.status) for v in res.authority_layer] == [("owned", "leads")]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_scalar_authority_rejects_repository_ineligible_projection_without_new_kwargs(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    _setup_search_and_metadata(stub_graphiti_client, stub_memory_graph_adapter)
    stub_memory_graph_adapter.search_assertion_embeddings = (
        lambda vec, *, limit, namespaces: _one_observation_hit()
    )
    calls: list[dict] = []

    def _fetch_view(*, subject_uuid, attribute, scope, value_kind, unit, namespace):
        calls.append({"subject_uuid": subject_uuid, "namespace": namespace})
        return {
            "uuid": "operator-view", "value": 37, "name": "operator-only current 37",
            "recall_eligible": False,
        }

    stub_memory_graph_adapter.fetch_current_scalar_view_for_slot = _fetch_view
    svc = _build_recall_service(
        stub_graphiti_client, stub_memory_graph_adapter, scalar_view_authority_enabled=True,
    )

    result = await svc.recall("how many rare coins now", namespace="proj")

    assert calls == [{"subject_uuid": "ent-coins", "namespace": "proj"}]
    assert "operator-only current 37" not in {row.name for row in result.results}
    assert not result.authority_layer


@pytest.mark.unit
@pytest.mark.asyncio
async def test_scalar_authority_stays_advisory_without_foundation(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    # Phase 4b foundation gate (G10): the current View is still injected + VISIBLE, but when its
    # effective tier is `agent`-only (probabilistic extraction, no source foundation), it must NOT be
    # marked the current authority -- it stays ADVISORY. This is the governance posture today (all
    # perception is agent-tier) until the G14 :TurnEvidence declarant bridge makes a foundation reachable.
    _setup_search_and_metadata(stub_graphiti_client, stub_memory_graph_adapter)
    stub_memory_graph_adapter.search_assertion_embeddings = (
        lambda vec, *, limit, namespaces: _one_observation_hit()
    )
    stub_memory_graph_adapter.fetch_current_scalar_view_for_slot = (
        lambda *, subject_uuid, attribute, scope, value_kind, unit, namespace: {
            "uuid": "view-owned-current", "value": 37, "name": "user's owned: 37. current owned = 37.",
            "attribute": "owned", "subject_uuid": subject_uuid, "namespace": "proj",
            "valid_at": "2026-07-02T00:00:00+00:00",
            "recall_eligible": True,
        }
    )
    stub_memory_graph_adapter.scalar_state_service = lambda: SimpleNamespace(
        current_authority=lambda subj, *, namespace, as_of: {("owned", "", "count", ""): "agent"})
    svc = _build_recall_service(
        stub_graphiti_client, stub_memory_graph_adapter, scalar_view_authority_enabled=True)

    res = await svc.recall("how many rare coins now", namespace="proj")

    by_name = {r.name: r for r in res.results}
    assert "user's owned: 37. current owned = 37." in by_name        # injected + visible (additive)
    assert by_name["user's owned: 37. current owned = 37."].is_scalar_authority is False  # advisory


@pytest.mark.unit
@pytest.mark.asyncio
async def test_scalar_authority_leads_on_user_founds_at_agent_tier(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    # G14 slice 3 (the bridge payoff): the effective tier is `agent` (probabilistic extraction, NOT a
    # FOUNDATION tier), but the View's head traces to a declarant='user' :TurnEvidence admission
    # (scalar_view_has_user_foundation=True), so it LEADS -- an admitted user statement is a real source
    # foundation INDEPENDENT of the agent extraction tier (10.G).
    _setup_search_and_metadata(stub_graphiti_client, stub_memory_graph_adapter)
    stub_memory_graph_adapter.search_assertion_embeddings = (
        lambda vec, *, limit, namespaces: _one_observation_hit()
    )
    stub_memory_graph_adapter.fetch_current_scalar_view_for_slot = (
        lambda *, subject_uuid, attribute, scope, value_kind, unit, namespace: {
            "uuid": "view-owned-current", "value": 37, "name": "user's owned: 37. current owned = 37.",
            "attribute": "owned", "subject_uuid": subject_uuid, "namespace": "proj",
            "valid_at": "2026-07-02T00:00:00+00:00",
            "recall_eligible": True,
        }
    )
    stub_memory_graph_adapter.scalar_state_service = lambda: SimpleNamespace(
        current_authority=lambda subj, *, namespace, as_of: {("owned", "", "count", ""): "agent"})
    founds_calls: list[dict] = []

    def _founds(*, view_uuid, namespace):
        founds_calls.append({"view_uuid": view_uuid, "namespace": namespace})
        return True

    stub_memory_graph_adapter.scalar_view_has_user_foundation = _founds
    svc = _build_recall_service(
        stub_graphiti_client, stub_memory_graph_adapter, scalar_view_authority_enabled=True)

    res = await svc.recall("how many rare coins now", namespace="proj")

    by_name = {r.name: r for r in res.results}
    assert by_name["user's owned: 37. current owned = 37."].is_scalar_authority is True  # leads
    assert founds_calls and founds_calls[0]["view_uuid"] == "view-owned-current"


def _self_and_dad_hits():
    return [
        {"assertion_id": "obs-self", "stated_span": "I own 20 rare coins", "cosine": 0.99,
         "subject_uuid": "ent-self", "subject_display": "user", "attribute": "owned", "scope": "",
         "value_kind": "count", "unit": "", "namespace": "proj", "evidence_tier": "agent",
         "operation": "absolute", "value": "20", "valid_at": "2026-07-01T00:00:00+00:00"},
        {"assertion_id": "obs-dad", "stated_span": "my dad owns 5 rare coins", "cosine": 0.95,
         "subject_uuid": "ent-dad", "subject_display": "dad", "attribute": "owned", "scope": "",
         "value_kind": "count", "unit": "", "namespace": "proj", "evidence_tier": "agent",
         "operation": "absolute", "value": "5", "valid_at": "2026-07-01T00:00:00+00:00"},
    ]


def _setup_two_subject_authority(stub_graphiti_client, stub_memory_graph_adapter):
    _setup_search_and_metadata(stub_graphiti_client, stub_memory_graph_adapter)
    stub_memory_graph_adapter.search_assertion_embeddings = (
        lambda vec, *, limit, namespaces: _self_and_dad_hits()
    )
    view_calls: list[str] = []

    def _fetch_view(*, subject_uuid, attribute, scope, value_kind, unit, namespace):
        view_calls.append(subject_uuid)
        return {
            "uuid": f"view-{subject_uuid}", "value": 37 if subject_uuid == "ent-self" else 5,
            "name": f"{subject_uuid} owned current", "attribute": "owned",
            "subject_uuid": subject_uuid, "namespace": "proj",
            "valid_at": "2026-07-02T00:00:00+00:00",
            "recall_eligible": True,
        }

    stub_memory_graph_adapter.fetch_current_scalar_view_for_slot = _fetch_view
    # both subjects have a FOUNDATION tier, so ONLY the G17 subject gate can keep one out.
    stub_memory_graph_adapter.scalar_state_service = lambda: SimpleNamespace(
        current_authority=lambda subj, *, namespace, as_of: {("owned", "", "count", ""): "user"})
    return view_calls


@pytest.mark.unit
@pytest.mark.asyncio
async def test_scalar_authority_rejects_cross_subject_view(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    # G17 (4a.3): a FIRST-PERSON query resolves to the self subject, so a View about a DIFFERENT subject
    # (dad) that also embedding-matched must NOT be injected as authority -- only self's View leads.
    view_calls = _setup_two_subject_authority(stub_graphiti_client, stub_memory_graph_adapter)
    svc = _build_recall_service(
        stub_graphiti_client, stub_memory_graph_adapter, scalar_view_authority_enabled=True)

    res = await svc.recall("how many rare coins do I own now", namespace="proj")

    names = {r.name for r in res.results}
    assert "ent-self owned current" in names                    # self View injected + leads
    assert "ent-dad owned current" not in names                 # cross-subject View NOT injected
    assert "ent-dad" not in view_calls                          # never even looked up dad's View
    assert "my dad owns 5 rare coins" in names                  # dad's OBSERVATION still present (additive)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_scalar_authority_allows_named_third_party(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    # G17: a query that NAMES the third party ("my dad") rescues that subject -- dad's View may lead.
    _setup_two_subject_authority(stub_graphiti_client, stub_memory_graph_adapter)
    svc = _build_recall_service(
        stub_graphiti_client, stub_memory_graph_adapter, scalar_view_authority_enabled=True)

    res = await svc.recall("how many rare coins does my dad own now", namespace="proj")

    by_name = {r.name: r for r in res.results}
    assert by_name["ent-dad owned current"].is_scalar_authority is True   # named -> allowed to lead


def _expired_hit():
    return [{
        "assertion_id": "obs-exp", "stated_span": "I used to own 37 rare coins", "cosine": 0.98,
        "subject_uuid": "ent-self", "subject_display": "user", "attribute": "owned", "scope": "",
        "value_kind": "count", "unit": "", "namespace": "proj", "evidence_tier": "agent",
        "operation": "absolute", "value": "37", "valid_at": "2026-06-01T00:00:00+00:00",
    }]


def _setup_expired_slot(stub_graphiti_client, stub_memory_graph_adapter, *, founded):
    from menhir.domain.scalar_state_fold import Expiry
    _setup_search_and_metadata(stub_graphiti_client, stub_memory_graph_adapter)
    stub_memory_graph_adapter.search_assertion_embeddings = (
        lambda vec, *, limit, namespaces: _expired_hit()
    )
    # the slot has NO current View (expired) ...
    stub_memory_graph_adapter.fetch_current_scalar_view_for_slot = (
        lambda **kw: None
    )
    exp = Expiry(
        slot_key=("ent-self", "owned", "", "count", ""), subject_uuid="ent-self",
        subject_display="user", valid_at="2026-06-15T00:00:00+00:00", expired_value="37",
        contributor_ids=["exp-aid"], episode_uuids=[])
    stub_memory_graph_adapter.scalar_state_service = lambda: SimpleNamespace(
        current_authority=lambda subj, *, namespace, as_of: {},
        current_expiries=lambda subj, *, namespace, as_of: {("owned", "", "count", ""): exp})
    stub_memory_graph_adapter.assertions_have_user_foundation = (
        lambda *, assertion_ids, namespace: bool(founded)
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_scalar_expiry_verdict_leads_on_user_foundation(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    # G13 slice A: a current-state query on an EXPIRED slot returns the EXPIRY VERDICT (last-known +
    # current UNKNOWN), never the old value as current; it LEADS when the "used to own" was user-founded.
    _setup_expired_slot(stub_graphiti_client, stub_memory_graph_adapter, founded=True)
    svc = _build_recall_service(
        stub_graphiti_client, stub_memory_graph_adapter, scalar_view_authority_enabled=True)

    res = await svc.recall("how many rare coins do I own now", namespace="proj")

    verdict = next((r for r in res.results if "EXPIRED" in r.name), None)
    assert verdict is not None
    assert "current owned UNKNOWN" in verdict.name and "37" in verdict.name   # last-known, not current
    assert verdict.is_scalar_authority is True                                # user-founded -> leads
    assert "I used to own 37 rare coins" in {r.name for r in res.results}     # observation still present


@pytest.mark.unit
@pytest.mark.asyncio
async def test_scalar_expiry_verdict_advisory_without_foundation(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    # No user foundation -> the expiry verdict is still SURFACED (so the value is never read as current)
    # but stays advisory, not the authoritative verdict.
    _setup_expired_slot(stub_graphiti_client, stub_memory_graph_adapter, founded=False)
    svc = _build_recall_service(
        stub_graphiti_client, stub_memory_graph_adapter, scalar_view_authority_enabled=True)

    res = await svc.recall("how many rare coins do I own now", namespace="proj")

    verdict = next((r for r in res.results if "EXPIRED" in r.name), None)
    assert verdict is not None and verdict.is_scalar_authority is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_scalar_expiry_verdict_suppressed_for_historical_intent(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    # A PREVIOUS_VALUE / as-of query (history intent) must NOT get a current-UNKNOWN verdict -- the past
    # value is exactly what was asked (as-of fold is slice B). No expiry verdict is injected.
    _setup_expired_slot(stub_graphiti_client, stub_memory_graph_adapter, founded=True)
    svc = _build_recall_service(
        stub_graphiti_client, stub_memory_graph_adapter, scalar_view_authority_enabled=True)

    res = await svc.recall("how many rare coins did I used to own", namespace="proj")

    assert not any("EXPIRED" in r.name for r in res.results)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_scalar_asof_fold_leads_for_as_of_date_query(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    # G13 slice B: an explicit "as of <date>" query leads with the AS-OF FOLD at that date (20), NOT the
    # current View (37) -- and the current-View lookup is not even consulted.
    from menhir.domain.scalar_state_fold import FoldedScalarState, FoldResult
    _setup_search_and_metadata(stub_graphiti_client, stub_memory_graph_adapter)
    stub_memory_graph_adapter.search_assertion_embeddings = (
        lambda vec, *, limit, namespaces: _one_observation_hit()   # subject ent-coins, display "user"
    )
    current_view_calls: list[int] = []
    stub_memory_graph_adapter.fetch_current_scalar_view_for_slot = (
        lambda **kw: current_view_calls.append(1) or {
            "uuid": "v", "value": 37, "name": "current 37", "recall_eligible": True,
        }
    )
    st = FoldedScalarState(
        subject_uuid="ent-coins", subject_display="user", attribute="owned", scope="",
        value_kind="count", unit="", value=20, valid_at="2026-02-01T00:00:00+00:00",
        contributor_ids=["a-old"], effective_tier="agent", episode_uuids=[], anchor_value=20)
    stub_memory_graph_adapter.scalar_state_service = lambda: SimpleNamespace(
        fold_entity=lambda subj, *, namespace, as_of: FoldResult(states=[st]))
    stub_memory_graph_adapter.assertions_have_user_foundation = (
        lambda *, assertion_ids, namespace: True
    )
    svc = _build_recall_service(
        stub_graphiti_client, stub_memory_graph_adapter, scalar_view_authority_enabled=True)

    res = await svc.recall("how many rare coins did I own as of 2026-03-01", namespace="proj")

    asof = next((r for r in res.results if "as of 2026-03-01" in r.name), None)
    assert asof is not None and "20" in asof.name and asof.is_scalar_authority is True
    assert "current 37" not in {r.name for r in res.results}   # current View not injected
    assert current_view_calls == []                            # and never even looked up


@pytest.mark.unit
@pytest.mark.asyncio
async def test_authority_annotation_recall_audit_event_emitted(
    stub_graphiti_client, stub_memory_graph_adapter, monkeypatch
) -> None:
    # Phase 4b instrumentation: the additive authority path emits an `authority_annotation` recall_audit
    # event per injected slot (leads vs advisory + basis) + an `observation_lane` summary, so a MEASURE
    # run can compute the wrongful-authority rate. Here the tier is `agent` with no foundation -> advisory.
    events: list = []
    fake_audit = SimpleNamespace(
        begin=lambda *a, **k: "cid",
        audit=lambda event, state, **kw: events.append((event, state, kw.get("details", {}))),
    )
    monkeypatch.setattr("menhir.infrastructure.audit_trail.RECALL", fake_audit)
    _setup_search_and_metadata(stub_graphiti_client, stub_memory_graph_adapter)
    stub_memory_graph_adapter.search_assertion_embeddings = (
        lambda vec, *, limit, namespaces: _one_observation_hit()
    )
    stub_memory_graph_adapter.fetch_current_scalar_view_for_slot = (
        lambda **kw: {
            "uuid": "v", "value": 37, "name": "current 37", "recall_eligible": True,
        }
    )
    stub_memory_graph_adapter.scalar_state_service = lambda: SimpleNamespace(
        current_authority=lambda subj, *, namespace, as_of: {("owned", "", "count", ""): "agent"},
        current_expiries=lambda subj, *, namespace, as_of: {})
    svc = _build_recall_service(
        stub_graphiti_client, stub_memory_graph_adapter, scalar_view_authority_enabled=True)

    await svc.recall("how many rare coins now", namespace="proj")

    ann = [e for e in events if e[0] == "authority_annotation"]
    assert ann and ann[0][1] == "advisory" and ann[0][2]["kind"] == "current"   # agent -> advisory
    assert ann[0][2]["user_foundation"] is False
    lane = [e for e in events if e[0] == "observation_lane"]
    assert lane and lane[0][2]["authority_annotations"] >= 1   # summary recorded the lane fired
