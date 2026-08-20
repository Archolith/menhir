"""Unit tests for the TEMPORAL direct-write path (SSOT-02 namespace fix).

Covers TemporalRepository, its MemoryGraphAdapter delegate, and the add_memory
MCP tool's TEMPORAL branch. No live Neo4j: a stub captures the Cypher/params.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock

import pytest

from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
from menhir.infrastructure.temporal_repository import TemporalRepository


@dataclass
class _EchoNeo4j:
    """Stub Neo4j that records calls."""

    calls: list[dict[str, object]] = field(default_factory=list)

    def execute(self, query: str, params: dict[str, object] | None = None) -> list[dict[str, object]]:
        self.calls.append({"query": query, "params": params or {}})
        return []


@pytest.mark.unit
def test_create_temporal_defaults_to_shared_default_group() -> None:
    neo4j = _EchoNeo4j()
    repo = TemporalRepository(neo4j)

    repo.create_temporal(content="renew passport", target_date="2027-02-16")

    params = neo4j.calls[0]["params"]
    assert params["group_id"] == ""
    assert params["namespace"] == "default"


@pytest.mark.unit
def test_create_temporal_stamps_explicit_namespace() -> None:
    neo4j = _EchoNeo4j()
    repo = TemporalRepository(neo4j)

    repo.create_temporal(
        content="private reminder", target_date="2027-02-16", namespace="private-ns",
    )

    params = neo4j.calls[0]["params"]
    # group_id is the load-bearing graphiti partition; namespace is the
    # defense-in-depth stamp recall's candidate filter reads.
    assert params["group_id"] == "private-ns"
    assert params["namespace"] == "private-ns"


@pytest.mark.unit
def test_adapter_forwards_namespace_to_temporal_repository() -> None:
    neo4j = _EchoNeo4j()
    adapter = MemoryGraphAdapter(neo4j=neo4j)

    adapter.create_temporal(
        content="c", target_date="2027-02-16", namespace="alpha",
    )

    params = neo4j.calls[0]["params"]
    assert params["group_id"] == "alpha"
    assert params["namespace"] == "alpha"


@pytest.mark.unit
def test_add_memory_tool_forwards_namespace_on_temporal_branch() -> None:
    from menhir.mcp.tools.ingest.add_memory import AddMemoryTool

    backend = MagicMock()
    backend.create_temporal = AsyncMock(return_value={
        "uuid": "t-1",
        "target_date": "2027-02-16",
    })
    tool = AddMemoryTool()
    tool.get_backend = MagicMock(return_value=backend)

    result = asyncio.run(tool.endpoint(
        text="renew passport",
        type="TEMPORAL",
        valid_at="2027-02-16",
        namespace="private-ns",
    ))

    assert "Created TEMPORAL memory uuid=t-1" in result
    backend.create_temporal.assert_awaited_once_with(
        content="renew passport",
        target_date="2027-02-16",
        source="claude-code",
        flagged=False,
        namespace="private-ns",
        turn_evidence_uuid=None,
    )


@pytest.mark.unit
def test_add_memory_tool_temporal_branch_normalizes_blank_namespace_to_none() -> None:
    from menhir.mcp.tools.ingest.add_memory import AddMemoryTool

    backend = MagicMock()
    backend.create_temporal = AsyncMock(return_value={
        "uuid": "t-2",
        "target_date": "2027-02-16",
    })
    tool = AddMemoryTool()
    tool.get_backend = MagicMock(return_value=backend)

    asyncio.run(tool.endpoint(
        text="renew passport", type="TEMPORAL", valid_at="2027-02-16", namespace="",
    ))

    backend.create_temporal.assert_awaited_once_with(
        content="renew passport",
        target_date="2027-02-16",
        source="claude-code",
        flagged=False,
        namespace=None,
        turn_evidence_uuid=None,
    )


# ---------------------------------------------------------------------------
# list_in_window tenancy predicate (CF-106)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_list_in_window_unscoped_has_no_namespace_predicate() -> None:
    neo4j = _EchoNeo4j()
    repo = TemporalRepository(neo4j)

    repo.list_in_window(window_days=30)

    query = neo4j.calls[0]["query"]
    params = neo4j.calls[0]["params"]
    assert "coalesce(n.namespace" not in query
    assert params["namespace"] is None


@pytest.mark.unit
def test_list_in_window_scoped_injects_namespace_predicate() -> None:
    neo4j = _EchoNeo4j()
    repo = TemporalRepository(neo4j)

    repo.list_in_window(window_days=30, namespace="tenantA")

    query = neo4j.calls[0]["query"]
    params = neo4j.calls[0]["params"]
    assert "coalesce(n.namespace, 'default') = $namespace" in query
    assert params["namespace"] == "tenantA"


@pytest.mark.unit
def test_list_in_window_scoped_query_is_still_valid_shape() -> None:
    neo4j = _EchoNeo4j()
    repo = TemporalRepository(neo4j)

    repo.list_in_window(window_days=30, namespace="tenantA")

    query = neo4j.calls[0]["query"]
    assert query.index("coalesce(n.namespace") < query.index("RETURN")
