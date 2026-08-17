"""CF-157 regression tests: pinned MCP resource reads stay inside their namespace.

The resource endpoint signatures contain only URI-template arguments, so the tool
namespace injector cannot protect them. These tests prove both halves of the fix:
resources pass the server-side client pin into their read calls, and the selector
queries enforce that namespace in Cypher rather than filtering returned rows after
the fact.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from menhir.mcp import contracts
from menhir.mcp.contracts import BaseJsonResource
from menhir.mcp.resources import (
    MemoriesByScopeResource,
    MemoriesBySearchResource,
    MemoriesByTypeResource,
    MemoryByUuidResource,
    RecentMemoriesResource,
)
from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter


@dataclass
class _StubNeo4jRepository:
    calls: list[dict[str, object]] = field(default_factory=list)

    def execute(self, query: str, params: dict[str, object] | None = None) -> list[dict[str, object]]:
        self.calls.append({"query": query, "params": params or {}})
        return []


class _RecordingBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def fetch_recent_memories(
        self, limit: int = 20, namespace: str | None = None
    ) -> list[dict[str, Any]]:
        self.calls.append(("recent", {"limit": limit, "namespace": namespace}))
        return []

    async def fetch_memory_by_uuid(
        self, node_uuid: str, *, namespace: str | None = None
    ) -> dict[str, Any] | None:
        self.calls.append(("uuid", {"node_uuid": node_uuid, "namespace": namespace}))
        return None

    async def fetch_memories_by_scope(
        self, scope: str, limit: int = 20, *, namespace: str | None = None
    ) -> list[dict[str, Any]]:
        self.calls.append(
            ("scope", {"scope": scope, "limit": limit, "namespace": namespace})
        )
        return []

    async def recall(self, query: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("search", {"query": query, **kwargs}))
        return {"results": [], "candidates_evaluated": 0}

    async def fetch_memories_by_type(
        self, memory_type: str, limit: int = 20, *, namespace: str | None = None
    ) -> list[dict[str, Any]]:
        self.calls.append(
            ("type", {"memory_type": memory_type, "limit": limit, "namespace": namespace})
        )
        return []


@pytest.mark.unit
def test_pinned_client_scopes_all_content_reading_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The five content-reading resources must propagate the server-side pin."""
    backend = _RecordingBackend()
    monkeypatch.setattr(BaseJsonResource, "get_backend", lambda _self: backend)
    monkeypatch.setattr(contracts, "get_pinned_namespace", lambda: "tenant-a")
    monkeypatch.setattr(contracts, "get_request_tier", lambda: "readonly")
    monkeypatch.setattr(contracts, "request_uses_query_auth", lambda: False)

    async def _read_all() -> None:
        await RecentMemoriesResource().execute()
        await MemoryByUuidResource().execute("11111111-1111-4111-8111-111111111111")
        await MemoriesByScopeResource().execute("PERSISTENT")
        await MemoriesBySearchResource().execute("remembered fact")
        await MemoriesByTypeResource().execute("SEMANTIC")

    asyncio.run(_read_all())

    assert [name for name, _kwargs in backend.calls] == [
        "recent",
        "uuid",
        "scope",
        "search",
        "type",
    ]
    for _name, kwargs in backend.calls:
        assert kwargs["namespace"] == "tenant-a"


@pytest.mark.unit
def test_uuid_selector_namespace_is_enforced_in_cypher() -> None:
    neo4j = _StubNeo4jRepository()
    adapter = MemoryGraphAdapter(neo4j=neo4j)

    adapter.fetch_memory_by_uuid("node-1", namespace="tenant-a")

    assert "coalesce(n.namespace, 'default') = $namespace" in neo4j.calls[0]["query"]
    assert neo4j.calls[0]["params"] == {
        "node_uuid": "node-1",
        "namespace": "tenant-a",
    }


@pytest.mark.unit
def test_scope_selector_namespace_is_enforced_in_cypher() -> None:
    neo4j = _StubNeo4jRepository()
    adapter = MemoryGraphAdapter(neo4j=neo4j)

    adapter.fetch_memories_by_scope("PERSISTENT", limit=10, namespace="tenant-a")

    assert "coalesce(n.namespace, 'default') = $namespace" in neo4j.calls[0]["query"]
    assert neo4j.calls[0]["params"] == {
        "scope": "PERSISTENT",
        "limit": 10,
        "namespace": "tenant-a",
    }


@pytest.mark.unit
def test_type_selector_namespace_is_enforced_in_cypher() -> None:
    neo4j = _StubNeo4jRepository()
    adapter = MemoryGraphAdapter(neo4j=neo4j)

    adapter.fetch_memories_by_type("SEMANTIC", limit=10, namespace="tenant-a")

    assert "coalesce(n.namespace, 'default') = $namespace" in neo4j.calls[0]["query"]
    assert neo4j.calls[0]["params"] == {
        "memory_type": "SEMANTIC",
        "limit": 10,
        "namespace": "tenant-a",
    }
