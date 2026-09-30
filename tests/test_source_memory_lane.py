"""Unit coverage for the default-off source-memory recall lane."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from menhir.config import MemorySettings
from menhir.domain.recall import RecallResult, SourceMemory
from menhir.domain.retrieval_tuning import RetrievalTuningConfig
from menhir.infrastructure.memory_queries import MemoryQueryRepository
from menhir.services.context_builder import ContextBuilderService
from menhir.services.recall_service import RecallService
from menhir.services.scoring_service import ScoringService


# ---------------------------------------------------------------------------
# Read query: predicates, read-only, limit clamp (mirrors test_content_vector_retrieval)
# ---------------------------------------------------------------------------


@dataclass
class _RecordingRepository:
    rows: list[dict[str, object]] = field(default_factory=list)
    calls: list[tuple[str, dict[str, object] | None]] = field(default_factory=list)

    def execute(self, query: str, params: dict[str, object] | None = None):
        self.calls.append((query, params))
        return self.rows


def test_episode_embedding_query_has_predicates_and_is_read_only() -> None:
    neo4j = _RecordingRepository(
        rows=[{"uuid": "u1", "content": "c", "cosine": 0.7}]
    )
    repository = MemoryQueryRepository(neo4j)  # type: ignore[arg-type]

    rows = repository.search_episode_embeddings(
        [0.1, 0.2], limit=999, namespace="tenant-a"
    )

    assert rows == neo4j.rows
    query, params = neo4j.calls[0]
    assert "MATCH (n:Episodic)" in query
    assert "vector.similarity.cosine" in query
    # Shared predicates, not respelled here.
    assert "structure_role IS NULL" in query
    assert "is_evidence_projection" in query
    assert "view_class = 'FACT'" in query
    # Menhir-own queue nodes only; failed enrichments excluded.
    assert "n.processing_state IS NOT NULL" in query
    assert "n.processing_state <> 'FAILED'" in query
    # Stale-dimension embeddings are skipped, not errored.
    assert "size(n.content_embedding) = size($query_vector)" in query
    # Shared tenancy predicate via tenant_scope_cypher, always included.
    assert "$tenant_namespaces IS NULL OR coalesce(n.namespace, n.group_id, '') IN $tenant_namespaces" in query
    assert not any(
        word in query.upper() for word in (" CREATE ", " MERGE ", " SET ", " DELETE ")
    )
    assert params == {
        "query_vector": [0.1, 0.2],
        "tenant_namespaces": ["tenant-a"],
        "limit": 50,
    }


def test_episode_embedding_query_without_namespace_has_no_namespace_param() -> None:
    neo4j = _RecordingRepository()
    repository = MemoryQueryRepository(neo4j)  # type: ignore[arg-type]

    repository.search_episode_embeddings([0.1], limit=5, namespace=None)

    _query, params = neo4j.calls[0]
    assert params == {"query_vector": [0.1], "limit": 5, "tenant_namespaces": None}


def test_episode_embedding_query_empty_namespace_scopes_to_default_silo() -> None:
    from menhir.domain.namespace import namespace_spellings

    neo4j = _RecordingRepository()
    repository = MemoryQueryRepository(neo4j)  # type: ignore[arg-type]

    repository.search_episode_embeddings([0.1], limit=5, namespace="")

    _query, params = neo4j.calls[0]
    # '' is NOT "no filter": it resolves to the legacy/default silo spellings.
    assert params["tenant_namespaces"] == namespace_spellings("")
    assert params["tenant_namespaces"] == ["default", ""]


def test_backfill_listing_has_namespace_and_processing_state_predicates() -> None:
    from menhir.infrastructure.episode_repository import EpisodeRepository

    neo4j = _RecordingRepository(
        rows=[{"uuid": "u1", "content": "c"}]
    )
    repo = EpisodeRepository(neo4j=neo4j)  # type: ignore[arg-type]

    repo.list_episodes_missing_content_embedding("", limit=25)

    query, params = neo4j.calls[0]
    assert "n.content_embedding IS NULL" in query
    assert "n.processing_state IS NOT NULL" in query
    assert "n.processing_state <> 'FAILED'" in query
    assert "structure_role IS NULL" in query
    assert "is_evidence_projection" in query
    assert "coalesce(n.namespace, n.group_id, '') IN $tenant_namespaces" in query
    assert params["tenant_namespaces"] == ["default", ""]
    assert params["limit"] == 25


# ---------------------------------------------------------------------------
# Recall lane behavior through RecallService with stubs
# ---------------------------------------------------------------------------


def _meta(uuid: str, name: str) -> dict:
    from datetime import datetime, timezone

    return {
        "uuid": uuid,
        "name": name,
        "scope": "PERSISTENT",
        "type": "SEMANTIC",
        "content": "content",
        "summary": None,
        # Fixed, not now(): last_accessed_days_ago feeds the scored breakdown, and a
        # moving timestamp would make two recall calls differ for no good reason.
        "last_accessed": datetime(2026, 9, 1, tzinfo=timezone.utc),
        "edge_count": 1,
        "sharpness": 0.1,
        "freshness": "ACTIVE",
        "user_flagged": False,
    }


def _episode_row(uuid: str, content: str, reference_time: str, cosine: float) -> dict:
    return {
        "uuid": uuid,
        "content": content,
        "source": "claude-code",
        "session_id": "s1",
        "reference_time": reference_time,
        "cosine": cosine,
    }


def _identity(result: Any) -> list[tuple[str, str, str | None]]:
    """Wall-clock-stable identity of a recall result's ranked list."""
    return [(r.uuid, r.name, r.content) for r in result.results]


def _setup(stub_graphiti_client, stub_memory_graph_adapter) -> None:
    stub_graphiti_client.search_scored_results = [("entity-1", "Entity One", 0.9)]
    stub_memory_graph_adapter.candidate_metadata = [_meta("entity-1", "Entity One")]


def _svc(stub_graphiti_client, stub_memory_graph_adapter) -> RecallService:
    return RecallService(
        graphiti_client=stub_graphiti_client,
        graph_adapter=stub_memory_graph_adapter,
        scoring_service=ScoringService(),
    )


def _tuning(**overrides: Any) -> RetrievalTuningConfig:
    return RetrievalTuningConfig(**overrides)


@pytest.mark.asyncio
async def test_flag_off_returns_none_and_makes_no_calls(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    _setup(stub_graphiti_client, stub_memory_graph_adapter)
    baseline = await _svc(stub_graphiti_client, stub_memory_graph_adapter).recall(
        "query", tuning=_tuning()
    )

    on = await _svc(stub_graphiti_client, stub_memory_graph_adapter).recall(
        "query",
        tuning=_tuning(enable_source_memories=True, source_memory_limit=10),
        include_session=True,
    )

    assert baseline.source_memories is None
    assert on.source_memories == ()  # lane ran, but the stub returned no rows
    # recency terms carry wall-clock deltas between the two calls; identity + order
    # + candidate count are the load-bearing "identical results" contract here.
    assert _identity(baseline) == _identity(on)
    assert baseline.candidates_evaluated == on.candidates_evaluated
    # Flag-OFF run made zero embed or episode-search calls for the feature; the
    # flag-ON run made exactly one episode search (which found nothing in the stub).
    assert len(stub_memory_graph_adapter.episode_embedding_calls) == 1
    assert stub_graphiti_client.embed_query_calls == ["query"]


@pytest.mark.asyncio
async def test_flag_on_section_present_oldest_first_and_capped(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    _setup(stub_graphiti_client, stub_memory_graph_adapter)
    stub_memory_graph_adapter.episode_embedding_results = [
        _episode_row("ep-new", "x" * 700, "2026-09-02", 0.9),
        _episode_row("ep-old", "old memory", "2026-01-01", 0.8),
    ]
    svc = _svc(stub_graphiti_client, stub_memory_graph_adapter)

    result = await svc.recall(
        "query",
        tuning=_tuning(enable_source_memories=True, source_memory_max_chars=600),
        include_session=True,
    )

    section = result.source_memories
    assert section is not None and len(section) == 2
    # Oldest first by reference_time, then uuid.
    assert [sm.uuid for sm in section] == ["ep-old", "ep-new"]
    assert section[0].content == "old memory"
    assert section[1].content.endswith("…") and len(section[1].content) == 601
    # Ranked results identical with the lane off (same query, lane adds nothing).
    baseline = await _svc(stub_graphiti_client, stub_memory_graph_adapter).recall(
        "query", tuning=_tuning()
    )
    assert _identity(result) == _identity(baseline)
    assert result.candidates_evaluated == baseline.candidates_evaluated


@pytest.mark.asyncio
async def test_per_call_zero_disables_and_positive_enables(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    _setup(stub_graphiti_client, stub_memory_graph_adapter)
    stub_memory_graph_adapter.episode_embedding_results = [
        _episode_row("ep-1", "a", "2026-01-01", 0.9),
        _episode_row("ep-2", "b", "2026-02-01", 0.8),
        _episode_row("ep-3", "c", "2026-03-01", 0.7),
    ]
    svc = _svc(stub_graphiti_client, stub_memory_graph_adapter)

    off = await svc.recall(
        "query",
        tuning=_tuning(enable_source_memories=True),
        source_memory_limit=0,
        include_session=True,
    )
    assert off.source_memories is None
    assert stub_memory_graph_adapter.episode_embedding_calls == []

    on = await svc.recall(
        "query", tuning=_tuning(), source_memory_limit=3, include_session=True
    )
    assert on.source_memories is not None and len(on.source_memories) == 3
    assert stub_memory_graph_adapter.episode_embedding_calls[-1]["limit"] == 3


@pytest.mark.asyncio
async def test_lane_failure_degrades_to_note_not_error(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    _setup(stub_graphiti_client, stub_memory_graph_adapter)

    async def _boom(vector):
        raise RuntimeError("embedder down")

    stub_graphiti_client.embed_query = _boom
    svc = _svc(stub_graphiti_client, stub_memory_graph_adapter)

    result = await svc.recall(
        "query", tuning=_tuning(enable_source_memories=True), include_session=True
    )

    assert result.results, "ranked results must survive a lane failure"
    assert result.source_memories is None
    assert result.note and "Source-memory lane unavailable" in result.note


@pytest.mark.asyncio
async def test_lane_runs_on_no_candidates_early_return(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    stub_graphiti_client.search_scored_results = []
    stub_memory_graph_adapter.candidate_metadata = []
    stub_memory_graph_adapter.episode_embedding_results = [
        _episode_row("ep-1", "found anyway", "2026-01-01", 0.9)
    ]
    svc = _svc(stub_graphiti_client, stub_memory_graph_adapter)

    result = await svc.recall(
        "query", tuning=_tuning(enable_source_memories=True), include_session=True
    )

    assert result.results == []
    assert result.candidates_evaluated == 0
    assert result.source_memories is not None
    assert [sm.uuid for sm in result.source_memories] == ["ep-1"]


@pytest.mark.asyncio
async def test_include_session_false_disables_the_lane(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    _setup(stub_graphiti_client, stub_memory_graph_adapter)
    stub_memory_graph_adapter.episode_embedding_results = [
        _episode_row("ep-1", "a", "2026-01-01", 0.9)
    ]
    svc = _svc(stub_graphiti_client, stub_memory_graph_adapter)

    result = await svc.recall(
        "query",
        tuning=_tuning(enable_source_memories=True),
        include_session=False,
    )

    assert result.source_memories is None
    assert stub_memory_graph_adapter.episode_embedding_calls == []


@pytest.mark.asyncio
async def test_ingest_step_swallows_embed_timeout(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    from menhir.services.enrichment_steps import embed_episode_content

    async def _hang(_text):
        raise TimeoutError("embedder hung past the 10s bound")

    stub_graphiti_client.embed_query = _hang
    ctx = _ctx(
        stub_graphiti_client,
        stub_memory_graph_adapter,
        {"uuid": "ep-1", "content": "hello"},
        enabled=True,
    )
    # Timeout is swallowed like any other lane error; enrichment continues.
    await embed_episode_content(ctx)
    assert stub_memory_graph_adapter.episode_embeddings_written == {}


@pytest.mark.asyncio
async def test_context_builder_appends_source_memories_section(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    from menhir.domain.retrieval_trace_models import RelevanceBreakdown
    from menhir.domain.recall import ScoredMemory

    result = RecallResult(
        query="q",
        preset="knowledge",
        results=[
            ScoredMemory(
                uuid="e1", name="E1", content="c", scope="PERSISTENT",
                memory_type="SEMANTIC", final_score=1.0,
                breakdown=RelevanceBreakdown(
                    semantic_similarity=1.0, adjacency_bonus=0.0, recency_bonus=0.0,
                    prominence_bonus=0.0, conflict_bonus=0.0, type_boost=0.0,
                    preset="knowledge", alpha=0.2, beta=0.1, gamma=0.1, delta=0.0,
                ),
            )
        ],
        candidates_evaluated=1,
        nodes_touched=0,
        source_memories=(
            SourceMemory(
                uuid="ep-1", content="used cd 3", reference_time="2026-01-01",
                cosine=0.9, source="claude-code",
            ),
        ),
    )

    class _StubRecallService:
        async def recall(self, *args, **kwargs):
            return result

    builder = ContextBuilderService(recall_service=_StubRecallService())
    out = await builder.build_context("q")

    assert "Source memories" in out.context
    assert "used cd 3" in out.context
    assert "2026-01-01" in out.context


# ---------------------------------------------------------------------------
# Pools (pure module)
# ---------------------------------------------------------------------------


def _sm(uuid: str, content: str) -> SourceMemory:
    return SourceMemory(
        uuid=uuid, content=content, reference_time=None, cosine=1.0, source=None
    )


def test_extract_anchors_ignores_step_labels_and_finds_identities() -> None:
    from menhir.domain.source_memory_pools import extract_anchors

    anchors = extract_anchors("edited src/app.py at step 3; run cd 3; see test_foo_bar")
    assert "src/app.py" in anchors
    assert "test_foo_bar" in anchors
    assert "cd 3" in anchors
    assert not any(a.startswith("step") for a in anchors)


def test_pools_deterministic_and_disjoint() -> None:
    from menhir.domain.source_memory_pools import assign_pools

    memories = [
        _sm("a", "touched src/app.py and src/util.py"),
        _sm("b", "rewrote src/app.py for the fix"),
        _sm("c", "unrelated memory about nothing"),
    ]
    forward = assign_pools(list(memories))
    backward = assign_pools(list(reversed(memories)))

    ids_forward = {m.uuid: pid for m, pid, _anchor in forward if pid}
    ids_backward = {m.uuid: pid for m, pid, _anchor in backward if pid}
    assert ids_forward == ids_backward
    assert set(ids_forward) == {"a", "b"}  # 'c' shares no anchor with >= 2
    # Each memory in at most one pool.
    assert len([1 for _m, pid, _a in forward if pid]) == 2


def test_pools_ignores_single_memory_anchors() -> None:
    from menhir.domain.source_memory_pools import assign_pools

    memories = [_sm("a", "only one file src/unique.py"), _sm("b", "different entirely")]
    results = assign_pools(memories)
    assert all(pid is None for _m, pid, _anchor in results)


# ---------------------------------------------------------------------------
# Ingest step
# ---------------------------------------------------------------------------


def _ctx(stub_graphiti_client, stub_memory_graph_adapter, claimed, *, enabled) -> Any:
    from menhir.services.enrichment_steps import EnrichmentContext
    from menhir.services.ingest_gate import IngestGate

    return EnrichmentContext(
        episode_uuid=str(claimed.get("uuid") or "ep-1"),
        claimed=claimed,
        started=0.0,
        processing_attempts=0,
        worker_id="w",
        graph_adapter=stub_memory_graph_adapter,
        graphiti_client=stub_graphiti_client,
        lifecycle_service=None,
        llm=None,
        ingest_gate=IngestGate(1),
        processing_steps_total=5,
        settings_record_revisions=False,
        ready_warning_ms=0,
        graphiti_add_episode_timeout_s=30.0,
        graphiti_episode_max_estimated_tokens=12000,
        get_queue_depth=lambda: 0,
        source_memories_enabled=enabled,
    )


@pytest.mark.asyncio
async def test_ingest_step_skipped_when_setting_off(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    from menhir.services.enrichment_steps import embed_episode_content

    ctx = _ctx(
        stub_graphiti_client,
        stub_memory_graph_adapter,
        {"uuid": "ep-1", "content": "hello"},
        enabled=False,
    )
    await embed_episode_content(ctx)

    assert stub_graphiti_client.embed_query_calls == []
    assert stub_memory_graph_adapter.episode_embeddings_written == {}


@pytest.mark.asyncio
async def test_ingest_step_embeds_once_and_skips_existing(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    from menhir.services.enrichment_steps import embed_episode_content

    claimed = {"uuid": "ep-1", "content": "hello world"}
    ctx = _ctx(stub_graphiti_client, stub_memory_graph_adapter, claimed, enabled=True)
    await embed_episode_content(ctx)

    assert stub_graphiti_client.embed_query_calls == ["hello world"]
    _vec, model = stub_memory_graph_adapter.episode_embeddings_written["ep-1"]
    assert isinstance(model, str) and model

    # Already-embedded episodes are skipped (idempotent across retries).
    calls_before = list(stub_graphiti_client.embed_query_calls)
    await embed_episode_content(ctx)
    assert stub_graphiti_client.embed_query_calls == calls_before


@pytest.mark.asyncio
async def test_ingest_step_skips_evidence_projections(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    from menhir.services.enrichment_steps import embed_episode_content

    ctx = _ctx(
        stub_graphiti_client,
        stub_memory_graph_adapter,
        {"uuid": "ep-1", "content": "raw turn", "is_evidence_projection": True},
        enabled=True,
    )
    await embed_episode_content(ctx)

    assert stub_graphiti_client.embed_query_calls == []
    assert stub_memory_graph_adapter.episode_embeddings_written == {}


@pytest.mark.asyncio
async def test_ingest_step_swallows_embedder_errors(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    from menhir.services.enrichment_steps import embed_episode_content

    async def _boom(_text):
        raise RuntimeError("embedder exploded")

    stub_graphiti_client.embed_query = _boom
    ctx = _ctx(
        stub_graphiti_client,
        stub_memory_graph_adapter,
        {"uuid": "ep-1", "content": "hello"},
        enabled=True,
    )
    # Must not raise: enrichment continues.
    await embed_episode_content(ctx)
    assert stub_memory_graph_adapter.episode_embeddings_written == {}


# ---------------------------------------------------------------------------
# Settings: env parsing and range validation
# ---------------------------------------------------------------------------


def test_settings_defaults_are_off(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "MENHIR_FRONTIER_SOURCE_MEMORIES",
        "MENHIR_FRONTIER_SOURCE_MEMORY_LIMIT",
        "MENHIR_FRONTIER_SOURCE_MEMORY_MAX_CHARS",
        "MENHIR_FRONTIER_SOURCE_MEMORY_POOLS",
    ):
        monkeypatch.delenv(var, raising=False)
    settings = MemorySettings.from_env()
    assert settings.frontier_source_memories is False
    assert settings.frontier_source_memory_limit == 10
    assert settings.frontier_source_memory_max_chars == 600
    assert settings.frontier_source_memory_pools is False

    tuning = settings.retrieval_tuning()
    assert tuning.enable_source_memories is False
    assert tuning.source_memory_limit == 10
    assert tuning.source_memory_max_chars == 600
    assert tuning.source_memory_pools is False


def test_settings_env_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MENHIR_FRONTIER_SOURCE_MEMORIES", "true")
    monkeypatch.setenv("MENHIR_FRONTIER_SOURCE_MEMORY_LIMIT", "25")
    monkeypatch.setenv("MENHIR_FRONTIER_SOURCE_MEMORY_MAX_CHARS", "1200")
    monkeypatch.setenv("MENHIR_FRONTIER_SOURCE_MEMORY_POOLS", "1")
    settings = MemorySettings.from_env()
    assert settings.frontier_source_memories is True
    assert settings.frontier_source_memory_limit == 25
    assert settings.frontier_source_memory_max_chars == 1200
    assert settings.frontier_source_memory_pools is True


def test_settings_env_bounds_fail_at_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MENHIR_FRONTIER_SOURCE_MEMORY_LIMIT", "99")
    with pytest.raises(ValueError):
        MemorySettings.from_env()
    monkeypatch.setenv("MENHIR_FRONTIER_SOURCE_MEMORY_LIMIT", "10")
    monkeypatch.setenv("MENHIR_FRONTIER_SOURCE_MEMORY_MAX_CHARS", "9999")
    with pytest.raises(ValueError):
        MemorySettings.from_env()


def test_tuning_range_validation() -> None:
    with pytest.raises(ValueError):
        RetrievalTuningConfig(source_memory_limit=0)
    with pytest.raises(ValueError):
        RetrievalTuningConfig(source_memory_limit=51)
    with pytest.raises(ValueError):
        RetrievalTuningConfig(source_memory_max_chars=50)
    with pytest.raises(ValueError):
        RetrievalTuningConfig(source_memory_max_chars=5000)
    # Bounds are inclusive.
    RetrievalTuningConfig(source_memory_limit=1, source_memory_max_chars=4000)
    RetrievalTuningConfig(source_memory_limit=50, source_memory_max_chars=100)


# ---------------------------------------------------------------------------
# API + MCP surfaces
# ---------------------------------------------------------------------------


def test_recall_request_source_memory_limit_bounds() -> None:
    from menhir.api.routes_support import RecallRequest
    from pydantic import ValidationError

    assert RecallRequest(query="q").source_memory_limit is None
    assert RecallRequest(query="q", source_memory_limit=0).source_memory_limit == 0
    assert RecallRequest(query="q", source_memory_limit=50).source_memory_limit == 50
    with pytest.raises(ValidationError):
        RecallRequest(query="q", source_memory_limit=51)
    with pytest.raises(ValidationError):
        RecallRequest(query="q", source_memory_limit=-1)


def test_recall_response_omits_source_memories_when_none() -> None:
    from menhir.api.routes_support import RecallResponse

    payload = RecallResponse(
        query="q", preset="knowledge", results=[], candidates_evaluated=0,
    ).model_dump(exclude_none=True)
    assert "source_memories" not in payload

    payload = RecallResponse(
        query="q", preset="knowledge", results=[], candidates_evaluated=0,
        source_memories=[{
            "uuid": "ep-1", "content": "c", "reference_time": None,
            "cosine": 0.5, "source": None, "pool_id": None, "pool_anchor": None,
        }],
    ).model_dump(exclude_none=True)
    assert payload["source_memories"][0]["uuid"] == "ep-1"


@pytest.mark.asyncio
async def test_mcp_payload_keeps_source_memories_key(
    monkeypatch: pytest.MonkeyPatch, stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    from menhir.mcp.tools.recall.recall_memories import RecallMemoriesTool

    async def _recall(*args, **kwargs):
        return {
            "preset": "knowledge",
            "results": [],
            "candidates_evaluated": 0,
            "source_memories": [
                {"uuid": "ep-1", "content": "c", "reference_time": "2026-01-01",
                 "cosine": 0.5, "source": None, "pool_id": None, "pool_anchor": None}
            ],
        }

    tool = RecallMemoriesTool()
    monkeypatch.setattr(tool, "get_backend", lambda: SimpleNamespaceBackend(_recall))

    raw = await tool.endpoint("query", source_memory_limit=3)
    payload = json.loads(raw)
    assert payload["source_memories"][0]["uuid"] == "ep-1"


class SimpleNamespaceBackend:
    def __init__(self, recall) -> None:
        self._recall = recall

    async def recall(self, *args, **kwargs):
        return await self._recall(*args, **kwargs)
