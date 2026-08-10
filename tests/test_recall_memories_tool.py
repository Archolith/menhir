"""Rendering contract for the MCP recall tool's structured authority layer."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from menhir.mcp.tools.recall.recall_memories import RecallMemoriesTool


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
