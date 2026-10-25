"""source_memory_sources: keep only source memories whose `:Episodic.source` is listed.

Covers the normalizer, the request model, the route/client/runtime/service threading, the Cypher
shape, the lane's call shape, and (online) the before-LIMIT semantics against a real Neo4j.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from menhir.api import routes as api_routes
from menhir.api.routes import router
from menhir.api.routes_support import RecallRequest
from menhir.core.backend_client_ops import BackendClientOpsMixin
from menhir.core.backend_runtime_data_ops import RuntimeProviderDataOpsMixin
from menhir.domain.recall import (
    MAX_SOURCE_MEMORY_SOURCE_CHARS,
    MAX_SOURCE_MEMORY_SOURCES,
    normalize_source_memory_sources,
)
from menhir.domain.retrieval_tuning import RetrievalTuningConfig
from menhir.infrastructure.episode_repository import EpisodeRepository
from menhir.infrastructure.memory_queries import MemoryQueryRepository
from menhir.services.recall_pipeline import _build_source_memories


# --------------------------------------------------------------------------- 1. normalizer


@pytest.mark.unit
def test_normalizer_strips_dedupes_and_keeps_order():
    assert normalize_source_memory_sources([" agent-writer ", "user", "agent-writer", "user "]) == (
        "agent-writer",
        "user",
    )
    assert normalize_source_memory_sources(("b", "a")) == ("b", "a")
    assert isinstance(normalize_source_memory_sources(["x"]), tuple)


@pytest.mark.unit
@pytest.mark.parametrize(
    "bad",
    [
        "agent-writer",
        b"agent-writer",
        {"agent-writer"},
        None,
        42,
        [],
        [""],
        ["   "],
        [1],
        ["ok", None],
        ["x" * (MAX_SOURCE_MEMORY_SOURCE_CHARS + 1)],
        [f"s{i}" for i in range(MAX_SOURCE_MEMORY_SOURCES + 1)],
    ],
)
def test_normalizer_rejects_bad_input(bad):
    with pytest.raises(ValueError):
        normalize_source_memory_sources(bad)


@pytest.mark.unit
def test_normalizer_boundaries_are_accepted():
    assert len(normalize_source_memory_sources([f"s{i}" for i in range(MAX_SOURCE_MEMORY_SOURCES)])) == 20
    item = "x" * MAX_SOURCE_MEMORY_SOURCE_CHARS
    assert normalize_source_memory_sources([item]) == (item,)
    # Duplicates collapse before the count cap is applied.
    assert normalize_source_memory_sources(["a"] * 30) == ("a",)


# --------------------------------------------------------------------------- 2. RecallRequest


@pytest.mark.unit
def test_recall_request_source_memory_sources_default_and_cleaning():
    assert RecallRequest(query="q").source_memory_sources is None
    req = RecallRequest(query="q", source_memory_sources=[" agent-writer ", "agent-writer", "user"])
    assert req.source_memory_sources == ["agent-writer", "user"]


@pytest.mark.unit
@pytest.mark.parametrize(
    "bad",
    [
        [],
        [""],
        [f"s{i}" for i in range(21)],
        ["x" * 201],
        "agent-writer",
    ],
)
def test_recall_request_rejects_bad_source_memory_sources(bad):
    with pytest.raises(ValidationError):
        RecallRequest(query="q", source_memory_sources=bad)


# --------------------------------------------------------------------------- 3. Cypher shape


@dataclass
class _Recorder:
    calls: list[tuple[str, dict[str, Any] | None]] = field(default_factory=list)

    def execute(self, query: str, params: dict[str, Any] | None = None):
        self.calls.append((query, params))
        return []


def _run_query(**kw):
    rec = _Recorder()
    MemoryQueryRepository(rec).search_episode_embeddings([0.1, 0.2], limit=5, namespace="ns", **kw)  # type: ignore[arg-type]
    return rec.calls[0]


@pytest.mark.unit
def test_cypher_filters_on_sources_when_given():
    query, params = _run_query(sources=("agent-writer",))
    assert "n.source IN $sources" in query
    assert params["sources"] == ["agent-writer"]
    # The filter lives in WHERE, i.e. before the LIMIT.
    assert query.index("n.source IN $sources") < query.index("LIMIT $limit")


@pytest.mark.unit
def test_cypher_without_sources_has_no_clause_or_param():
    query, params = _run_query()
    assert "$sources" not in query
    assert "sources" not in params
    query2, params2 = _run_query(sources=None)
    assert "$sources" not in query2 and "sources" not in params2


@pytest.mark.unit
def test_cypher_empty_sources_fails_closed():
    query, params = _run_query(sources=())
    assert "n.source IN $sources" in query
    assert params["sources"] == []


# --------------------------------------------------------------------------- 4. lane call shape


class _Graphiti:
    embedder_ref = None

    async def embed_query(self, _q: str) -> list[float]:
        return [1.0, 0.0]


class _AdapterWithSources:
    def __init__(self):
        self.calls: list[dict[str, Any]] = []

    def search_episode_embeddings(self, vector, *, limit, namespace, sources=None, **kw):
        self.calls.append({"limit": limit, "namespace": namespace, "kw": kw, "has_sources": "sources" in kw or sources is not None, "sources": sources})
        return []


class _AdapterWithoutSources:
    """Signature lacks `sources`: passing it would raise TypeError (the lane would degrade)."""

    def __init__(self):
        self.calls = 0

    def search_episode_embeddings(self, vector, *, limit, namespace, **kw):
        assert "sources" not in kw
        self.calls += 1
        return [{"uuid": "u1", "content": "c", "source": "user", "cosine": 0.5}]


async def _lane(adapter, *, limit, sources, enable=False):
    service = SimpleNamespace(graphiti_client=_Graphiti(), graph_adapter=adapter)
    return await _build_source_memories(
        service, "q", "ns",
        RetrievalTuningConfig(enable_source_memories=enable, source_memory_pools=False),
        limit, include_session=True, content_query_vector=[1.0, 0.0], sources=sources,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_lane_passes_sources_to_adapter():
    adapter = _AdapterWithSources()
    await _lane(adapter, limit=3, sources=("agent-writer",))
    assert adapter.calls[0]["sources"] == ("agent-writer",)
    assert adapter.calls[0]["limit"] == 3


@pytest.mark.unit
@pytest.mark.asyncio
async def test_lane_without_sources_does_not_pass_the_kwarg():
    adapter = _AdapterWithoutSources()
    section, note, _ms = await _lane(adapter, limit=3, sources=None)
    assert note is None, note
    assert adapter.calls == 1
    assert section is not None and [sm.uuid for sm in section] == ["u1"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sources_alone_do_not_enable_the_lane():
    adapter = _AdapterWithSources()
    section, note, _ms = await _lane(adapter, limit=None, sources=("agent-writer",), enable=False)
    assert (section, note) == (None, None)
    assert adapter.calls == []
    # Control: the same call with a positive limit does run the lane.
    await _lane(adapter, limit=2, sources=("agent-writer",), enable=False)
    assert len(adapter.calls) == 1


# --------------------------------------------------------------------------- 5. route


@pytest.mark.unit
def test_route_passes_source_memory_sources_only_when_set():
    backend = SimpleNamespace(recall=AsyncMock(return_value={
        "query": "q", "preset": "knowledge", "results": [], "candidates_evaluated": 0}))
    app = FastAPI()
    app.include_router(router)
    with patch.object(api_routes, "_get_backend", return_value=backend):
        http = TestClient(app)
        assert http.post("/api/recall", json={"query": "q"}).status_code == 200
        assert "source_memory_sources" not in backend.recall.call_args.kwargs
        resp = http.post("/api/recall", json={"query": "q", "source_memory_sources": ["agent-writer"]})
        assert resp.status_code == 200
        assert backend.recall.call_args.kwargs["source_memory_sources"] == ["agent-writer"]
        n_calls = backend.recall.call_count
        assert http.post("/api/recall", json={"query": "q", "source_memory_sources": []}).status_code == 422
        assert backend.recall.call_count == n_calls


# --------------------------------------------------------------------------- 6. runtime ops


def _runtime_ops(recall):
    ops = object.__new__(RuntimeProviderDataOpsMixin)
    ops.built = SimpleNamespace(settings=SimpleNamespace(), recall_service=SimpleNamespace(recall=recall))
    ops._effective_session_id = lambda: None
    return ops


@pytest.mark.unit
@pytest.mark.asyncio
async def test_runtime_ops_normalize_and_forward_sources_only_when_set():
    recall = AsyncMock(return_value={"query": "q", "results": []})
    ops = _runtime_ops(recall)
    await ops.recall("q")
    assert "source_memory_sources" not in recall.call_args.kwargs
    await ops.recall("q", source_memory_sources=["agent-writer", "agent-writer"])
    assert recall.call_args.kwargs["source_memory_sources"] == ("agent-writer",)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_runtime_ops_reject_bare_string_before_calling_service():
    recall = AsyncMock(return_value={"query": "q", "results": []})
    ops = _runtime_ops(recall)
    with pytest.raises(ValueError):
        await ops.recall("q", source_memory_sources="agent-writer")  # type: ignore[arg-type]
    recall.assert_not_called()


# --------------------------------------------------------------------------- 7. client ops


@pytest.mark.unit
@pytest.mark.asyncio
async def test_client_ops_send_source_memory_sources_only_when_set():
    ops = object.__new__(BackendClientOpsMixin)
    ops._request = AsyncMock(return_value={})
    await ops.recall("q")
    assert "source_memory_sources" not in ops._request.call_args.args[1]
    await ops.recall("q", source_memory_sources=["agent-writer"])
    assert ops._request.call_args.args[1]["source_memory_sources"] == ["agent-writer"]


# --------------------------------------------------------------------------- 8. ONLINE

Q = [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
MODEL = "embed-model-v1"


def _vec(x: float, y: float) -> list[float]:
    return [x, y, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]


def _seed(graph: Any, uuid: str, source: str, vec: list[float], namespace: str = "ns-src") -> None:
    repo = EpisodeRepository(graph)
    repo.create_pending_episode(
        episode_uuid=uuid, name=f"name-{uuid}", content=f"note {uuid}", session_id="sess-1",
        user_id="user-1", source=source, source_confidence=0.9, namespace=namespace,
        reference_time=None,
    )
    assert repo.set_episode_content_embedding(uuid, vec, MODEL) is True


def _search(graph: Any, **kw: Any) -> list[str]:
    rows = MemoryQueryRepository(graph).search_episode_embeddings(
        Q, namespace="ns-src", model=MODEL, **kw
    )
    return [str(r["uuid"]) for r in rows]


@pytest.mark.online
def test_online_source_filter_applies_before_limit(test_neo4j_repo: Any) -> None:
    """Breaks if the source predicate is applied after LIMIT (or dropped / not passed)."""
    graph = test_neo4j_repo
    # user episodes are MORE similar to Q than agent-writer ones.
    for i, y in enumerate((0.01, 0.02, 0.03)):
        _seed(graph, f"user-{i}", "user", _vec(1.0, y))
    for i, y in enumerate((0.5, 0.6)):
        _seed(graph, f"aw-{i}", "agent-writer", _vec(1.0, y))

    # Control: unfiltered query returns rows (not vacuous), and the top 2 are user episodes.
    assert len(_search(graph, limit=50)) == 5
    assert _search(graph, limit=2) == ["user-0", "user-1"]

    filtered = _search(graph, limit=2, sources=("agent-writer",))
    assert sorted(filtered) == ["aw-0", "aw-1"]

    both = _search(graph, limit=50, sources=("agent-writer", "user"))
    assert len(both) == 5

    assert _search(graph, limit=2, sources=("nope",)) == []
    assert _search(graph, limit=2, sources=()) == []
