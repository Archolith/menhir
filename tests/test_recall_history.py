"""Coverage for the recall_history drill-down (service, REST, MCP, registries)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from menhir.domain.recall import RecallHistoryResult
from menhir.services.recall_service import RecallService
from menhir.services.scoring_service import ScoringService


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


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_time_order_oldest_first_regardless_of_cosine_order(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    stub_memory_graph_adapter.episode_embedding_results = [
        _episode_row("ep-new", "newest", "2026-09-02", 0.95),
        _episode_row("ep-old", "oldest", "2026-01-01", 0.60),
        _episode_row("ep-mid", "middle", "2026-05-01", 0.80),
    ]
    result = await _svc(stub_graphiti_client, stub_memory_graph_adapter).recall_history(
        "how did auth change"
    )
    assert [sm.uuid for sm in result.memories] == ["ep-old", "ep-mid", "ep-new"]
    assert result.note is None
    assert result.query == "how did auth change"


@pytest.mark.asyncio
async def test_limit_clamped_to_read_query_bounds(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    svc = _svc(stub_graphiti_client, stub_memory_graph_adapter)
    await svc.recall_history("q", limit=999)
    assert stub_memory_graph_adapter.episode_embedding_calls[-1]["limit"] == 50
    await svc.recall_history("q", limit=-5)
    assert stub_memory_graph_adapter.episode_embedding_calls[-1]["limit"] == 1


@pytest.mark.asyncio
async def test_pools_on_and_off(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    stub_memory_graph_adapter.episode_embedding_results = [
        _episode_row("ep-1", "touched src/app.py for the fix", "2026-01-01", 0.9),
        _episode_row("ep-2", "rewrote src/app.py later", "2026-02-01", 0.8),
        _episode_row("ep-3", "unrelated content entirely", "2026-03-01", 0.7),
    ]
    svc = _svc(stub_graphiti_client, stub_memory_graph_adapter)

    pooled = await svc.recall_history("q", pools=True)
    by_uuid = {sm.uuid: sm for sm in pooled.memories}
    assert by_uuid["ep-1"].pool_id and by_uuid["ep-1"].pool_id == by_uuid["ep-2"].pool_id
    assert by_uuid["ep-3"].pool_id is None

    unpooled = await svc.recall_history("q", pools=False)
    assert all(sm.pool_id is None for sm in unpooled.memories)


@pytest.mark.asyncio
async def test_empty_result_sets_note(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    stub_memory_graph_adapter.episode_embedding_results = []
    result = await _svc(stub_graphiti_client, stub_memory_graph_adapter).recall_history("q")
    assert result.memories == ()
    assert result.note is not None
    assert "backfill_episode_embeddings" in result.note


@pytest.mark.asyncio
async def test_empty_query_raises_value_error(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    svc = _svc(stub_graphiti_client, stub_memory_graph_adapter)
    with pytest.raises(ValueError):
        await svc.recall_history("   ")
    assert stub_graphiti_client.embed_query_calls == []


@pytest.mark.asyncio
async def test_embed_failure_propagates(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    async def _boom(_text):
        raise RuntimeError("embedder down")

    stub_graphiti_client.embed_query = _boom
    with pytest.raises(RuntimeError):
        await _svc(stub_graphiti_client, stub_memory_graph_adapter).recall_history("q")


@pytest.mark.asyncio
async def test_no_access_update_calls(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    stub_memory_graph_adapter.episode_embedding_results = [
        _episode_row("ep-1", "c", "2026-01-01", 0.9)
    ]
    svc = _svc(stub_graphiti_client, stub_memory_graph_adapter)
    await svc.recall_history("q")
    # The drill-down is a pure read: exactly one embedding search, and none of the
    # post-recall write side-effects (edge reinforcement, lifecycle, deletes) fired.
    assert len(stub_memory_graph_adapter.episode_embedding_calls) == 1
    assert stub_memory_graph_adapter.weighted_edges == []
    assert stub_memory_graph_adapter.deleted_nodes == []
    assert stub_memory_graph_adapter.touch_count == 0


@pytest.mark.asyncio
async def test_content_whitespace_collapse_and_full_content_cap(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    long = "a b  " + ("x" * 5000)
    stub_memory_graph_adapter.episode_embedding_results = [
        _episode_row("ep-1", long, "2026-01-01", 0.9)
    ]
    result = await _svc(stub_graphiti_client, stub_memory_graph_adapter).recall_history("q")
    content = result.memories[0].content
    assert content.startswith("a b ")
    assert len(content) == 4001 and content.endswith("…")


@pytest.mark.asyncio
async def test_result_is_frozen_dataclass(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    stub_memory_graph_adapter.episode_embedding_results = [
        _episode_row("ep-1", "c", "2026-01-01", 0.9)
    ]
    result = await _svc(stub_graphiti_client, stub_memory_graph_adapter).recall_history("q")
    assert isinstance(result, RecallHistoryResult)
    with pytest.raises(Exception):
        result.memories = ()  # type: ignore[misc]


# ---------------------------------------------------------------------------
# REST
# ---------------------------------------------------------------------------


def test_recall_history_request_bounds() -> None:
    from pydantic import ValidationError

    from menhir.api.routes_support import RecallHistoryRequest

    req = RecallHistoryRequest(query="q")
    assert req.limit == 30 and req.pools is True and req.namespace is None
    assert RecallHistoryRequest(query="q", limit=1).limit == 1
    assert RecallHistoryRequest(query="q", limit=50).limit == 50
    with pytest.raises(ValidationError):
        RecallHistoryRequest(query="q", limit=51)
    with pytest.raises(ValidationError):
        RecallHistoryRequest(query="q", limit=0)
    # An empty query is not a request-validation error; the service raises ValueError.


def test_rest_whitespace_query_returns_422() -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from menhir.api import routes as api_routes
    from menhir.api.routes import router

    backend = SimpleNamespace()
    backend.recall_history = AsyncMock(side_effect=ValueError("query must not be empty"))
    app = FastAPI()
    app.include_router(router)
    with patch.object(api_routes, "_get_backend", return_value=backend):
        resp = TestClient(app).post("/api/recall/history", json={"query": "   "})
    assert resp.status_code == 422
    assert "non-empty" in resp.json()["detail"] or "empty" in resp.json()["detail"]


def test_recall_history_response_shape_and_note_omission() -> None:
    from menhir.api.routes_support import RecallHistoryResponse

    payload = RecallHistoryResponse(
        query="q",
        memories=[{
            "uuid": "ep-1", "content": "c", "reference_time": "2026-01-01",
            "cosine": 0.5, "source": None, "pool_id": None, "pool_anchor": None,
        }],
    ).model_dump(exclude_none=True)
    assert payload["memories"][0]["uuid"] == "ep-1"
    assert "note" not in payload

    empty = RecallHistoryResponse(query="q", memories=[], note="nothing matched")
    assert empty.note == "nothing matched"


# ---------------------------------------------------------------------------
# MCP
# ---------------------------------------------------------------------------


class _HistoryBackend:
    def __init__(self, result: dict[str, Any]) -> None:
        self._result = result

    async def recall_history(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self._result


@pytest.mark.asyncio
async def test_mcp_payload_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    from menhir.mcp.tools.recall.recall_history import RecallHistoryTool

    result = {
        "query": "q",
        "memories": [
            {"uuid": "ep-1", "content": "c", "reference_time": "2026-01-01",
             "cosine": 0.5, "source": "claude-code", "pool_id": None,
             "pool_anchor": None},
            {"uuid": "ep-2", "content": "d", "reference_time": "2026-02-01",
             "cosine": 0.4, "source": None, "pool_id": "pool:abc",
             "pool_anchor": "src/app.py"},
        ],
    }
    tool = RecallHistoryTool()
    monkeypatch.setattr(tool, "get_backend", lambda: _HistoryBackend(result))
    raw = await tool.endpoint("q")
    payload = json.loads(raw)
    assert payload["query"] == "q"
    assert payload["count"] == 2
    assert payload["memories"][0] == {
        "time": "2026-01-01", "content": "c", "uuid": "ep-1", "source": "claude-code",
    }
    assert payload["memories"][1]["pool_id"] == "pool:abc"
    assert payload["memories"][1]["pool_anchor"] == "src/app.py"
    assert "note" not in payload


@pytest.mark.asyncio
async def test_mcp_note_present_when_nothing_matched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from menhir.mcp.tools.recall.recall_history import RecallHistoryTool

    result = {"query": "q", "memories": [], "note": "No embedded memories matched."}
    tool = RecallHistoryTool()
    monkeypatch.setattr(tool, "get_backend", lambda: _HistoryBackend(result))
    payload = json.loads(await tool.endpoint("q"))
    assert payload["count"] == 0
    assert payload["note"] == "No embedded memories matched."


@pytest.mark.asyncio
async def test_mcp_whitespace_query_returns_error_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from menhir.mcp.tools.recall.recall_history import RecallHistoryTool

    backend = _HistoryBackend({})
    error = ValueError("query must not be empty")

    async def _raise(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise error

    backend.recall_history = _raise  # type: ignore[method-assign]
    tool = RecallHistoryTool()
    monkeypatch.setattr(tool, "get_backend", lambda: backend)
    payload = json.loads(await tool.endpoint("   "))
    assert payload["ok"] is False
    assert payload["tool"] == "recall_history"
    assert "non-empty" in payload["error"]["message"]


def test_registered_in_recall_tools() -> None:
    from menhir.mcp.tools.recall import RECALL_TOOLS

    assert any(getattr(t, "name", "") == "recall_history" for t in RECALL_TOOLS)


def test_registered_but_not_always_visible() -> None:
    import asyncio

    from menhir.mcp import server as mcp_server

    registered = {t.name for t in asyncio.run(mcp_server.mcp._list_tools())}
    assert "recall_history" in registered
    visible = {t.name for t in asyncio.run(mcp_server.mcp.list_tools())}
    assert "recall_history" not in visible


def test_tool_policy_attributes() -> None:
    from menhir.mcp.contracts import ToolScope
    from menhir.mcp.tools.recall.recall_history import RecallHistoryTool

    tool = RecallHistoryTool()
    assert tool.scope == ToolScope.NAMESPACED
    assert tool.required_tier == "readonly"
    assert tool.oauth_scopes == ("menhir:read",)
    assert tool.read_only_hint is True
    assert tool.destructive_hint is False


# ---------------------------------------------------------------------------
# Registries
# ---------------------------------------------------------------------------


def test_in_agent_allowed_tools() -> None:
    from menhir.access_contract import AGENT_ALLOWED_TOOLS

    assert "recall_history" in AGENT_ALLOWED_TOOLS


def test_in_feature_taxonomy() -> None:
    from menhir.explorer.feature_taxonomy import PARENTS

    assert "recall_history" in PARENTS["retrieve"]


def test_in_ratable_operations() -> None:
    from menhir.mcp.feedback import RATABLE_OPERATIONS

    assert "recall_history" in RATABLE_OPERATIONS
