"""Rendering contract for the MCP recall tool's structured authority layer."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from menhir.mcp.tools.recall.recall_memories import RecallMemoriesTool


@pytest.mark.asyncio
@pytest.mark.parametrize("compact", [False, True])
@pytest.mark.parametrize("state,expected_note", [
    ({"status": "completed", "type": "TEMPORAL"}, "not an outstanding obligation"),
    ({"artifact_status": "historical", "superseded_by": "new-artifact"}, "not current guidance"),
    ({"superseded_by": "new-artifact"}, "not current guidance"),
])
async def test_generic_lifecycle_state_survives_public_recall(
    stub_graphiti_client, stub_memory_graph_adapter, state, expected_note, compact,
) -> None:
    from datetime import datetime, timezone

    from menhir.core.backend_runtime import RuntimeProvider
    from menhir.services.recall_service import RecallService
    from menhir.services.scoring_service import ScoringService

    row = {
        "uuid": "historical", "name": "Submit report", "content": "Submit report by Friday",
        "type": "SEMANTIC", "scope": "PERSISTENT", "namespace": "tenant-a",
        "freshness": "ACTIVE", "last_accessed": datetime.now(timezone.utc),
        "edge_count": 1, "sharpness": 0.1, **state,
    }
    stub_graphiti_client.search_scored_results = [("historical", "Submit report", 0.85)]
    stub_memory_graph_adapter.candidate_metadata = [row]
    service = RecallService(
        graphiti_client=stub_graphiti_client, graph_adapter=stub_memory_graph_adapter,
        scoring_service=ScoringService(),
    )
    runtime = RuntimeProvider(SimpleNamespace(recall_service=service), process_session=None)
    tool = RecallMemoriesTool()
    tool.get_backend = MagicMock(return_value=runtime)
    payload = json.loads(await tool.endpoint(query="Submit report", namespace="tenant-a", compact=compact))
    item, = payload["items"]
    assert item["uuid"] == "historical"  # Historical records remain searchable.
    for key in ("status", "artifact_status", "superseded_by"):
        if key in state:
            assert item[key] == state[key]
    assert expected_note in item["lifecycle_note"]
    assert item["summary"] == "Submit report by Friday"


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("compact", [False, True])
async def test_mcp_recall_retains_bounded_authority_layer_in_all_modes(compact: bool) -> None:
    backend = MagicMock()
    backend.recall = AsyncMock(return_value={
        "preset": "knowledge", "results": [], "candidates_evaluated": 0,
        "authority_layer": [{
            "kind": "current", "status": "leads", "subject_uuid": "ent-self",
            "attribute": "owned", "scope": "", "value_kind": "count", "unit": "",
            "value": 37, "valid_at": "2026-07-22T00:00:00Z", "view_uuid": "view-37",
            "has_foundation": True,
            "contributors": [{
                "assertion_id": "a37", "relation": "CURRENT_ANCHOR",
                "operation": "absolute", "value": 37,
                "stated_span": "I own 37 rare coins",
                "valid_at": "2026-07-22T00:00:00Z", "evidence_tier": "user",
                "episode_uuid": "ep37",
            }],
            "contributors_total": 1, "contributors_truncated": False,
            "next_offset": None,
        }],
    })
    tool = RecallMemoriesTool()
    tool.get_backend = MagicMock(return_value=backend)

    rendered = await tool.endpoint(query="how many now", compact=compact)
    payload = json.loads(rendered)

    assert payload["authority_layer"][0]["status"] == "leads"
    assert payload["authority_layer"][0]["contributors"][0]["relation"] == "CURRENT_ANCHOR"


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("compact", [False, True])
async def test_mcp_recall_retains_event_authority_layer_in_all_modes(compact: bool) -> None:
    backend = MagicMock()
    backend.recall = AsyncMock(return_value={
        "preset": "knowledge", "results": [], "candidates_evaluated": 0,
        "event_authority_layer": [{
            "predicate": "acquired", "object_key": "red notebook",
            "object_display": "a red notebook", "valid_at": "2026-07-22T09:30:00Z",
            "stated_span": "I bought a red notebook.", "assertion_key": "asrt-7",
            "episode_uuid": "ep-7", "turn_evidence_uuid": "te-7", "domain": "stationery",
            "time_basis": "explicit", "status": "leads", "gate": "pass",
            "reason": "unique grounded lead", "subject_uuid": "ent-self",
            "has_foundation": True, "kind": "latest",
        }],
    })
    tool = RecallMemoriesTool()
    tool.get_backend = MagicMock(return_value=backend)

    rendered = await tool.endpoint(query="did I acquire a red notebook today?", compact=compact)
    payload = json.loads(rendered)

    assert payload["event_authority_layer"][0]["status"] == "leads"
    assert payload["event_authority_layer"][0]["predicate"] == "acquired"


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("compact", [False, True])
async def test_mcp_recall_omits_event_authority_when_absent(compact: bool) -> None:
    backend = MagicMock()
    backend.recall = AsyncMock(return_value={
        "preset": "knowledge", "results": [], "candidates_evaluated": 0,
        "event_authority_layer": None,
    })
    tool = RecallMemoriesTool()
    tool.get_backend = MagicMock(return_value=backend)

    rendered = await tool.endpoint(query="did I acquire a red notebook today?", compact=compact)
    payload = json.loads(rendered)

    assert "event_authority_layer" not in payload


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("compact", [False, True])
@pytest.mark.parametrize("caller_id,process_id", [("A", "B"), (None, "A"), (None, None)])
async def test_public_mcp_recall_keeps_effective_session_identity(
    monkeypatch, stub_graphiti_client, stub_memory_graph_adapter, compact, caller_id, process_id,
) -> None:
    from datetime import datetime, timezone

    from menhir.core.backend_runtime import RuntimeProvider
    from menhir.mcp import contracts
    from menhir.services.recall_service import RecallService
    from menhir.services.scoring_service import ScoringService

    rows = [
        ("own", "SESSION", "A"), ("foreign", "SESSION", "B"),
        ("ownerless", "SESSION", None), ("durable", "PERSISTENT", "B"),
    ]
    stub_graphiti_client.search_scored_results = [(uuid, uuid, 0.85) for uuid, _, _ in rows]
    stub_memory_graph_adapter.candidate_metadata = [
        {"uuid": uuid, "name": uuid, "scope": scope, "session_id": owner,
         "namespace": "tenant-a", "type": "SEMANTIC", "content": uuid,
         "freshness": "ACTIVE", "last_accessed": datetime.now(timezone.utc),
         "edge_count": 1, "sharpness": 0.1, "user_flagged": False}
        for uuid, scope, owner in rows
    ]
    svc = RecallService(
        graphiti_client=stub_graphiti_client, graph_adapter=stub_memory_graph_adapter,
        scoring_service=ScoringService(),
    )
    runtime = RuntimeProvider(
        SimpleNamespace(recall_service=svc), SimpleNamespace(session_id=process_id),
        caller_session=SimpleNamespace(session_id=caller_id) if caller_id is not None else None,
    )
    tool = RecallMemoriesTool()
    tool.get_backend = MagicMock(return_value=runtime)
    monkeypatch.setattr(contracts, "get_pinned_namespace", lambda: "tenant-a")
    monkeypatch.setattr(contracts, "get_request_tier", lambda: "readonly")
    monkeypatch.setattr(contracts, "require_trusted_client_identity", lambda: None)
    monkeypatch.setattr(contracts, "request_uses_query_auth", lambda: False)
    monkeypatch.setattr(contracts, "get_client_tool_allowlist", lambda: set())

    payload = json.loads(await tool.execute(query="memory", compact=compact, namespace="tenant-b"))
    returned = {row["uuid"] for row in payload.get("items", payload.get("results", []))}
    expected = {"own", "durable"} if caller_id or process_id else {uuid for uuid, _, _ in rows}
    assert returned == expected, payload
