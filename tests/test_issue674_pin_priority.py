"""#674: startup pins are ranked by an explicit priority, carry their pin scope, and a
workspace with no pins of its own says so instead of silently returning general pins."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from menhir.infrastructure.memory_queries import MemoryQueryRepository


class _RecordingNeo4j:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._rows = rows or []

    def execute(self, query: str, params: dict[str, Any] | None = None):
        self.calls.append((query, params or {}))
        return list(self._rows)


# --- write path -----------------------------------------------------------------


@pytest.mark.parametrize("bad", [-1, 101, True, "5", 2.5])
def test_invalid_pin_priority_refuses_before_any_graph_call(bad) -> None:
    neo4j = _RecordingNeo4j()
    with pytest.raises(ValueError, match="pin_priority"):
        MemoryQueryRepository(neo4j).flag_memory("node-1", pin_priority=bad)
    assert neo4j.calls == []


class _FlagNeo4j(_RecordingNeo4j):
    """The structural pre-check finds nothing; the flag write updates one node."""

    def execute(self, query: str, params: dict[str, Any] | None = None):
        self.calls.append((query, params or {}))
        return [] if "structure_role" in query else [{"nodes_updated": 1}]


def test_flag_with_priority_stores_it_and_flag_without_keeps_it() -> None:
    neo4j = _FlagNeo4j()
    repo = MemoryQueryRepository(neo4j)

    assert repo.flag_memory("node-1", bootstrap_scope="general", pin_priority=70)
    query, params = neo4j.calls[-1]
    assert "n.pin_priority = $pin_priority" in query
    assert params["pin_priority"] == 70

    assert repo.flag_memory("node-1")
    query, params = neo4j.calls[-1]
    assert "pin_priority" not in query
    assert "pin_priority" not in params


def test_unflag_clears_priority_so_a_later_flag_cannot_revive_it() -> None:
    neo4j = _RecordingNeo4j([{"nodes_updated": 1}])
    MemoryQueryRepository(neo4j).unflag_memory("node-1")
    assert "n.pin_priority = null" in neo4j.calls[-1][0]


def test_pin_counts_use_the_same_filter_as_the_pin_list() -> None:
    from menhir.infrastructure.memory_queries import _flagged_pin_filter

    neo4j = _RecordingNeo4j()
    repo = MemoryQueryRepository(neo4j)
    repo.fetch_flagged_memories(limit=10, workspace="ws", namespace="tenant-a")
    repo.fetch_flagged_pin_counts(workspace="ws", namespace="tenant-a")
    repo.fetch_flagged_memory_bootstrap_version(workspace="ws", namespace="tenant-a")
    clauses, expected_params = _flagged_pin_filter("ws", "tenant-a")
    assert expected_params["group_ids"] == ["tenant-a"]
    for query, params in neo4j.calls:
        assert all(clause in query for clause in clauses)
        assert {k: v for k, v in params.items() if k != "limit"} == expected_params


# --- MCP surfaces -----------------------------------------------------------------


def _bootstrap_backend(**overrides: Any) -> SimpleNamespace:
    backend = SimpleNamespace(
        fetch_flagged_memories=AsyncMock(return_value=[
            {"uuid": "pin-1", "content": "Rule", "scope": "SESSION", "type": "EPISODIC",
             "bootstrap_scope": "general", "pin_priority": 40},
            {"uuid": "pin-2", "content": "Rule 2", "bootstrap_scope": "general"},
        ]),
        fetch_flagged_memory_bootstrap_version=AsyncMock(return_value="workspace:ws:2:digest"),
        fetch_flagged_pin_counts=AsyncMock(return_value={"general": 2}),
    )
    for key, value in overrides.items():
        setattr(backend, key, value)
    return backend


async def _read_flagged(backend: SimpleNamespace, workspace: str) -> dict[str, Any]:
    from menhir.mcp.tools.recall.read_flagged_memories import ReadFlaggedMemoriesTool

    tool = ReadFlaggedMemoriesTool()
    with patch.object(type(tool), "get_backend", return_value=backend):
        return json.loads(await tool.endpoint(reader_id="r-674", workspace=workspace))


@pytest.mark.asyncio
async def test_items_carry_pin_scope_and_priority() -> None:
    payload = await _read_flagged(_bootstrap_backend(), "ws")
    first, second = payload["items"]
    assert (first["pin_scope"], first["pin_priority"]) == ("general", 40)
    assert (second["pin_scope"], second["pin_priority"]) == ("general", 0)
    # Existing lifecycle fields keep their meaning (renaming them would break agents).
    assert (first["scope"], first["type"]) == ("SESSION", "EPISODIC")


@pytest.mark.asyncio
async def test_workspace_without_pins_is_reported_not_silent() -> None:
    payload = await _read_flagged(_bootstrap_backend(), "ws")
    assert payload["workspace_pin_count"] == 0
    assert "workspace:ws" in payload["workspace_notice"]


@pytest.mark.asyncio
async def test_workspace_with_pins_has_no_notice() -> None:
    backend = _bootstrap_backend(
        fetch_flagged_pin_counts=AsyncMock(return_value={"general": 1, "workspace:ws": 1})
    )
    payload = await _read_flagged(backend, "ws")
    assert payload["workspace_pin_count"] == 1
    assert "workspace_notice" not in payload


@pytest.mark.asyncio
async def test_uncountable_workspace_is_unknown_never_zero() -> None:
    backend = _bootstrap_backend(
        fetch_flagged_pin_counts=AsyncMock(side_effect=RuntimeError("older server"))
    )
    payload = await _read_flagged(backend, "ws")
    assert payload["workspace_pin_count"] is None
    assert payload["workspace_notice"].startswith("Could not count")
    # The pins and the receipt still arrive.
    assert payload["bootstrap_ready"] is True
    assert len(payload["items"]) == 2


@pytest.mark.asyncio
async def test_general_read_has_no_workspace_fields() -> None:
    backend = _bootstrap_backend(
        fetch_flagged_memory_bootstrap_version=AsyncMock(return_value="general:2:digest")
    )
    payload = await _read_flagged(backend, "")
    assert "workspace_pin_count" not in payload
    backend.fetch_flagged_pin_counts.assert_not_awaited()


async def _flag(backend: SimpleNamespace, **kwargs: Any) -> str:
    from menhir.mcp.tools.ingest.flag_memory import FlagMemoryTool

    tool = FlagMemoryTool()
    with patch.object(type(tool), "get_backend", return_value=backend):
        return await tool.endpoint(node_uuid="node-1", **kwargs)


@pytest.mark.asyncio
async def test_flag_tool_refuses_out_of_range_priority_without_writing() -> None:
    from menhir.mcp.telemetry.tracker import McpToolRefusal

    backend = SimpleNamespace(flag_memory=AsyncMock(return_value=True),
                              fetch_memory_by_uuid=AsyncMock(return_value={}))
    with pytest.raises(McpToolRefusal, match="pin_priority"):
        await _flag(backend, pin_priority=101)
    backend.flag_memory.assert_not_awaited()


@pytest.mark.asyncio
async def test_flag_tool_forwards_priority() -> None:
    backend = SimpleNamespace(
        flag_memory=AsyncMock(return_value=True),
        fetch_memory_by_uuid=AsyncMock(return_value={"bootstrap_scope": "general"}),
    )
    text = await _flag(backend, bootstrap_scope="general", pin_priority=5)
    backend.flag_memory.assert_awaited_once_with(
        "node-1", bootstrap_scope="general", pin_priority=5
    )
    assert "pin_priority=5" in text


# --- live Neo4j: the real ordering and counts ---------------------------------------


@pytest.mark.online
def test_live_priority_then_workspace_then_recency(test_neo4j_repo) -> None:
    from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter

    neo4j = test_neo4j_repo
    ns = "issue674-" + uuid4().hex
    # (key, bootstrap_scope, last_accessed); priorities are set through flag_memory below.
    data = [
        ("general-newest", "general", "2003-01-01T00:00:00Z"),
        ("general-ranked", "general", "1999-01-01T00:00:00Z"),
        ("ws-newer", "workspace:ws", "2002-01-01T00:00:00Z"),
        ("ws-older", "workspace:ws", "2001-01-01T00:00:00Z"),
        ("other-ws", "workspace:elsewhere", "2004-01-01T00:00:00Z"),
    ]
    rows = [{"uuid": f"{ns}-{key}", "scope": scope, "accessed": accessed}
            for key, scope, accessed in data]
    uuids = [row["uuid"] for row in rows]
    try:
        neo4j.execute("""
            UNWIND $rows AS row
            CREATE (n:Episodic {uuid: row.uuid, name: row.uuid, content: 'Rule',
                namespace: $ns, group_id: $ns, type: 'EPISODIC', scope: 'SESSION',
                freshness: 'ACTIVE', user_flagged: true, bootstrap_scope: row.scope,
                created_at: datetime(row.accessed), last_accessed: datetime(row.accessed)})
        """, {"rows": rows, "ns": ns})
        adapter = MemoryGraphAdapter(neo4j=neo4j)
        assert adapter.flag_memory(f"{ns}-general-ranked", pin_priority=50)

        pins = adapter.fetch_flagged_memories(limit=10, workspace="ws", namespace=ns)
        expected = [f"{ns}-{key}" for key in
                    ["general-ranked", "ws-newer", "ws-older", "general-newest"]]
        assert [row["uuid"] for row in pins] == expected
        assert pins[0]["pin_priority"] == 50
        # A limit keeps the highest-ranked pins, not just the newest.
        assert [row["uuid"] for row in adapter.fetch_flagged_memories(
            limit=2, workspace="ws", namespace=ns)] == expected[:2]

        assert adapter.fetch_flagged_pin_counts(workspace="ws", namespace=ns) == {
            "general": 2, "workspace:ws": 2,
        }
        # A mistyped key matches no workspace pins: only general ones are counted.
        assert adapter.fetch_flagged_pin_counts(workspace="wz", namespace=ns) == {"general": 2}

        assert adapter.unflag_memory(f"{ns}-general-ranked")
        assert adapter.flag_memory(f"{ns}-general-ranked", bootstrap_scope="general")
        reflagged = adapter.fetch_flagged_memories(limit=10, workspace="ws", namespace=ns)
        assert reflagged[0]["uuid"] == f"{ns}-ws-newer"
        assert next(r for r in reflagged
                    if r["uuid"] == f"{ns}-general-ranked")["pin_priority"] is None
    finally:
        neo4j.execute("MATCH (n) WHERE n.uuid IN $uuids DETACH DELETE n", {"uuids": uuids})
