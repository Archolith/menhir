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
@pytest.mark.online
@pytest.mark.asyncio
async def test_lifecycle_transitions_survive_real_graph_generic_reads(test_neo4j_repo, stub_graphiti_client):
    """Use actual writers and graph projections; only semantic search seeds are stubbed."""
    from datetime import date
    from uuid import uuid4

    from menhir.infrastructure.artifact_repository import ArtifactRepository
    from menhir.mcp.formatters import _compact_memory_item, _compact_scored_item
    from menhir.services.recall_service import RecallService
    from menhir.services.scoring_service import ScoringService

    neo4j = test_neo4j_repo
    temporal = TemporalRepository(neo4j)
    artifacts = ArtifactRepository(neo4j)
    adapter = MemoryGraphAdapter(neo4j=neo4j)
    tag = "issue143-" + uuid4().hex
    artifact_ids = [tag + "-old", tag + "-new"]
    uuids = []
    try:
        reminder = temporal.create_temporal(content=tag + " submit report", target_date=date.today().isoformat())
        foreign = temporal.create_temporal(
            content=tag + " foreign report", target_date=date.today().isoformat(), namespace=tag,
        )
        uuids.extend([reminder["uuid"], foreign["uuid"]])
        for artifact_id in artifact_ids:
            created = artifacts.create_artifact(
                artifact_id=artifact_id, artifact_type="decision", summary=artifact_id,
                body="Use " + artifact_id, source="human", status="trusted",
                evidence=[{"kind": "test", "ref": artifact_id}],
            )
            assert created["status"] == "trusted"
            uuids.append(created["uuid"])
        old_uuid, new_uuid = uuids[2:]
        open_row = adapter.fetch_memory_by_uuid(reminder["uuid"], namespace="default")
        assert open_row["status"] == "open"
        assert "lifecycle_note" not in _compact_memory_item(open_row, tag="recent")
        assert "lifecycle_note" not in _compact_memory_item(
            adapter.fetch_memory_by_uuid(old_uuid, namespace="default"), tag="recent",
        )
        assert reminder["uuid"] in {row["uuid"] for row in temporal.list_in_window(namespace="default")}

        assert temporal.complete_temporal(reminder["uuid"])
        assert artifacts.supersede_artifact(*artifact_ids)
        assert reminder["uuid"] not in {row["uuid"] for row in temporal.list_in_window(namespace="default")}
        expected = {
            reminder["uuid"]: {"status": "completed"},
            old_uuid: {"artifact_status": "historical", "superseded_by": artifact_ids[1]},
            new_uuid: {"artifact_status": "trusted"},
        }
        # Every shared listing and explicit historical inspection uses the stored state.
        for rows in (
            adapter.fetch_recent_memories(limit=50, namespace="default"),
            adapter.fetch_memories_by_scope("PERSISTENT", limit=50, namespace="default"),
            [adapter.fetch_memory_by_uuid(node_uuid, namespace="default") for node_uuid in expected],
        ):
            by_uuid = {row["uuid"]: row for row in rows}
            assert foreign["uuid"] not in by_uuid
            for node_uuid, state in expected.items():
                for key, value in state.items():
                    assert by_uuid[node_uuid][key] == value
                item = _compact_memory_item(by_uuid[node_uuid], tag="recent")
                assert ("lifecycle_note" in item) == (node_uuid != new_uuid)
        stub_graphiti_client.search_scored_results = [(node_uuid, tag, .85) for node_uuid in uuids]
        recall = RecallService(graphiti_client=stub_graphiti_client, graph_adapter=adapter,
                               scoring_service=ScoringService())
        result = await recall.recall(tag, namespace="default", limit=10)
        items = {m.uuid: _compact_scored_item(m) for m in result.results}
        assert set(items) == set(expected)
        for node_uuid, state in expected.items():
            for key, value in state.items():
                assert items[node_uuid][key] == value
            assert ("lifecycle_note" in items[node_uuid]) == (node_uuid != new_uuid)
    finally:
        neo4j.execute("MATCH (n) WHERE n.uuid IN $uuids OR n.artifact_id IN $ids DETACH DELETE n",
                      {"uuids": uuids, "ids": artifact_ids})
