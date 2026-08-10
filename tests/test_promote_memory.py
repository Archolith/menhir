"""Unit tests for the PROMOTED tier writer path (SSOT-08).

Covers MemoryQueryRepository.promote_memory, its MemoryGraphAdapter delegate,
and the promote_memory MCP tool endpoint. No live Neo4j: a stub captures the
Cypher/params.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock

import pytest

from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
from menhir.infrastructure.memory_queries import MemoryQueryRepository


@dataclass
class _StubNeo4j:
    responses: list[list[dict[str, object]]] = field(default_factory=list)
    calls: list[dict[str, object]] = field(default_factory=list)

    def execute(self, query: str, params: dict[str, object] | None = None) -> list[dict[str, object]]:
        self.calls.append({"query": query, "params": params or {}})
        if self.responses:
            return self.responses.pop(0)
        return [{"nodes_updated": 1}]


@pytest.mark.unit
def test_promote_memory_sets_scope_and_pins_confidence() -> None:
    neo4j = _StubNeo4j()
    repo = MemoryQueryRepository(neo4j)

    result = repo.promote_memory("node-1")

    assert result is True
    query, params = neo4j.calls[0]["query"], neo4j.calls[0]["params"]
    assert "SET n.scope = 'PROMOTED'" in query
    assert "n.source_confidence = 1.0" in query
    assert params["node_uuid"] == "node-1"


@pytest.mark.unit
def test_promote_memory_guards_on_persistent_or_promoted_scope() -> None:
    neo4j = _StubNeo4j()
    repo = MemoryQueryRepository(neo4j)

    repo.promote_memory("node-1")

    query = neo4j.calls[0]["query"]
    assert "n.scope IN ['PERSISTENT', 'PROMOTED']" in query


@pytest.mark.unit
def test_promote_memory_returns_false_when_node_not_eligible() -> None:
    neo4j = _StubNeo4j(responses=[[{"nodes_updated": 0}]])
    repo = MemoryQueryRepository(neo4j)

    assert repo.promote_memory("session-node") is False


@pytest.mark.unit
def test_promote_memory_is_idempotent_on_already_promoted() -> None:
    # Guard includes 'PROMOTED' itself so re-promoting succeeds rather than
    # silently no-oping as "not found".
    neo4j = _StubNeo4j(responses=[[{"nodes_updated": 1}]])
    repo = MemoryQueryRepository(neo4j)

    assert repo.promote_memory("already-promoted") is True


@pytest.mark.unit
def test_adapter_delegates_promote_memory() -> None:
    neo4j = _StubNeo4j()
    adapter = MemoryGraphAdapter(neo4j=neo4j)

    assert adapter.promote_memory("node-1") is True
    assert "SET n.scope = 'PROMOTED'" in neo4j.calls[0]["query"]


@pytest.mark.unit
def test_promote_memory_tool_success_message() -> None:
    from menhir.mcp.tools.ingest.promote_memory import PromoteMemoryTool

    backend = MagicMock()
    backend.promote_memory = AsyncMock(return_value=True)
    tool = PromoteMemoryTool()
    tool.get_backend = MagicMock(return_value=backend)

    result = asyncio.run(tool.endpoint(node_uuid="node-1"))

    assert "Promoted memory node-1" in result
    backend.promote_memory.assert_awaited_once_with("node-1")


@pytest.mark.unit
def test_promote_memory_tool_not_found_message() -> None:
    from menhir.mcp.tools.ingest.promote_memory import PromoteMemoryTool

    backend = MagicMock()
    backend.promote_memory = AsyncMock(return_value=False)
    tool = PromoteMemoryTool()
    tool.get_backend = MagicMock(return_value=backend)

    result = asyncio.run(tool.endpoint(node_uuid="missing"))

    assert "No PERSISTENT memory found" in result


@pytest.mark.unit
def test_promote_memory_tool_requires_operator_tier() -> None:
    from menhir.mcp.tools.ingest.promote_memory import PromoteMemoryTool

    assert PromoteMemoryTool.required_tier == "operator"
