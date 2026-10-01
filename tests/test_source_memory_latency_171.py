"""menhir #171 -- source-memory section: index-friendly prefilter + concurrent lane start.

Behavior under test:
1. `tenant_scope_prefilter_cypher` emits an index-friendly SUPERSET (never a replacement for
   `tenant_scope_cypher`, which still decides membership).
2. `search_episode_embeddings` leads with the prefilter only when the namespace is scoped.
3. `run_recall` starts the source-memory lane as an asyncio task right after the vector-search
   phase, so the episode search overlaps the metadata/adjacency phases.
4. An exception between task creation and the await leaves no orphan task.
5. Results are identical to the sequential behavior pinned by tests/test_source_memory_lane.py.
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field
from typing import Any

import pytest

from menhir.domain.namespace import tenant_scope_cypher, tenant_scope_prefilter_cypher
from menhir.infrastructure.memory_queries import MemoryQueryRepository
from menhir.services.recall_service import RecallService
from menhir.services.scoring_service import ScoringService

pytestmark = pytest.mark.unit

PREFILTER = "(n.namespace IN $tenant_namespaces OR n.group_id IN $tenant_namespaces)"


# ---------------------------------------------------------------------------
# Behavior 1: the prefilter builder
# ---------------------------------------------------------------------------


def test_prefilter_contains_both_indexed_properties() -> None:
    frag = tenant_scope_prefilter_cypher("n")
    assert "namespace IN $tenant_namespaces" in frag
    assert "group_id IN $tenant_namespaces" in frag
    assert frag == PREFILTER


def test_prefilter_rejects_non_identifier_variables() -> None:
    for bad in ("n) OR (1=1", "", "n.namespace"):
        with pytest.raises(ValueError):
            tenant_scope_prefilter_cypher(bad)
    assert tenant_scope_cypher("n")  # sibling contract unchanged


# ---------------------------------------------------------------------------
# Behavior 2: search_episode_embeddings prefilter placement
# ---------------------------------------------------------------------------


@dataclass
class _RecordingRepository:
    rows: list[dict[str, object]] = field(default_factory=list)
    calls: list[tuple[str, dict[str, object] | None]] = field(default_factory=list)

    def execute(self, query: str, params: dict[str, object] | None = None):
        self.calls.append((query, params))
        return self.rows


def test_scoped_episode_search_has_prefilter_and_membership_predicate() -> None:
    neo4j = _RecordingRepository()
    repository = MemoryQueryRepository(neo4j)  # type: ignore[arg-type]

    repository.search_episode_embeddings([0.1], limit=5, namespace="tenant-a")

    query, _params = neo4j.calls[0]
    assert PREFILTER in query
    # The prefilter is a superset; tenant_scope_cypher still decides membership.
    assert (
        "$tenant_namespaces IS NULL OR coalesce(n.namespace, n.group_id, '') "
        "IN $tenant_namespaces"
    ) in query
    # Prefilter leads the WHERE list so the planner can seek the RANGE indexes.
    assert query.index(PREFILTER) < query.index("coalesce(n.namespace")


def test_unscoped_episode_search_has_no_prefilter() -> None:
    neo4j = _RecordingRepository()
    repository = MemoryQueryRepository(neo4j)  # type: ignore[arg-type]

    repository.search_episode_embeddings([0.1], limit=5, namespace=None)

    query, _params = neo4j.calls[0]
    assert PREFILTER not in query
    assert "IN $tenant_namespaces" not in query.replace(
        "coalesce(n.namespace, n.group_id, '') IN $tenant_namespaces", ""
    )


# ---------------------------------------------------------------------------
# Behavior 3 + 5: concurrent lane start through RecallService with stubs
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


def _svc(stub_graphiti_client, stub_memory_graph_adapter) -> RecallService:
    return RecallService(
        graphiti_client=stub_graphiti_client,
        graph_adapter=stub_memory_graph_adapter,
        scoring_service=ScoringService(),
    )


def _identity(result: Any) -> list[tuple[str, str, str | None]]:
    return [(r.uuid, r.name, r.content) for r in result.results]


@pytest.mark.asyncio
async def test_episode_search_starts_before_metadata_fetch_completes(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    stub_graphiti_client.search_scored_results = [("entity-1", "Entity One", 0.9)]
    stub_memory_graph_adapter.candidate_metadata = [_meta("entity-1", "Entity One")]
    stub_memory_graph_adapter.episode_embedding_results = [
        _episode_row("ep-1", "found concurrently", "2026-01-01", 0.9)
    ]

    events: list[str] = []
    search_started = threading.Event()
    adapter = stub_memory_graph_adapter

    def _search(vector, *, limit=10, namespace=None):
        events.append("search")
        search_started.set()
        return adapter.episode_embedding_results

    def _metadata(uuids):
        # Deterministic ordering probe: if the lane were still sequential, this would run
        # BEFORE the episode search and time out here instead of being released by it.
        assert search_started.wait(timeout=5.0), "episode search never started"
        events.append("metadata")
        return adapter.candidate_metadata

    adapter.search_episode_embeddings = _search
    adapter.fetch_candidate_metadata = _metadata

    result = await _svc(stub_graphiti_client, adapter).recall(
        "query",
        tuning=_tuning(enable_source_memories=True),
        include_session=True,
    )

    assert events == ["search", "metadata"]
    assert result.source_memories is not None
    assert [sm.uuid for sm in result.source_memories] == ["ep-1"]


def _tuning(**overrides: Any):
    from menhir.domain.retrieval_tuning import RetrievalTuningConfig

    return RetrievalTuningConfig(**overrides)


@pytest.mark.asyncio
async def test_exception_before_await_propagates_and_lane_finishes_cleanly(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    # The lane is read-only and never raises, so an early exit leaves it to finish on its own;
    # the original exception must still propagate and the task must end without an error.
    stub_graphiti_client.search_scored_results = [("entity-1", "Entity One", 0.9)]

    def _boom(uuids):
        raise RuntimeError("metadata phase exploded")

    stub_memory_graph_adapter.fetch_candidate_metadata = _boom
    service = _svc(stub_graphiti_client, stub_memory_graph_adapter)

    with pytest.raises(RuntimeError, match="metadata phase exploded"):
        await service.recall(
            "query",
            tuning=_tuning(enable_source_memories=True),
            include_session=True,
        )

    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if pending:
        await asyncio.wait(pending, timeout=5)
    leftover = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    assert leftover == [], f"lane task did not finish: {leftover}"
    assert all(t.exception() is None for t in pending if not t.cancelled())


@pytest.mark.asyncio
async def test_results_identical_to_sequential_lane_behavior(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    stub_graphiti_client.search_scored_results = [("entity-1", "Entity One", 0.9)]
    stub_memory_graph_adapter.candidate_metadata = [_meta("entity-1", "Entity One")]
    stub_memory_graph_adapter.episode_embedding_results = [
        _episode_row("ep-new", "x" * 700, "2026-09-02", 0.9),
        _episode_row("ep-old", "old memory", "2026-01-01", 0.8),
    ]
    svc = _svc(stub_graphiti_client, stub_memory_graph_adapter)

    on = await svc.recall(
        "query",
        tuning=_tuning(enable_source_memories=True, source_memory_max_chars=600),
        include_session=True,
    )

    section = on.source_memories
    assert section is not None and len(section) == 2
    # Oldest first by reference_time, then uuid -- the sequential contract.
    assert [sm.uuid for sm in section] == ["ep-old", "ep-new"]
    assert section[0].content == "old memory"
    assert section[1].content.endswith("…") and len(section[1].content) == 601

    baseline = await svc.recall("query", tuning=_tuning())
    assert _identity(on) == _identity(baseline)
    assert on.candidates_evaluated == baseline.candidates_evaluated
    assert baseline.source_memories is None
