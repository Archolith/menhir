"""Unit tests for the AddCandidateTool MCP tool endpoint."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_tool(backend: MagicMock) -> "AddCandidateTool":  # noqa: F821
    from menhir.mcp.tools.ingest.add_candidate import AddCandidateTool

    tool = AddCandidateTool()
    tool.get_backend = MagicMock(return_value=backend)
    return tool


# ---------------------------------------------------------------------------
# Created candidate
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_created_candidate() -> None:
    backend = MagicMock()
    backend.create_candidate = AsyncMock(return_value={
        "uuid": "c1",
        "scope": "CANDIDATE",
        "cluster_id": "cluster-42",
        "evidence_strength": "REPEATED",
        "distinct_sessions": 3,
        "created": True,
    })

    from menhir.mcp.tools.ingest.add_candidate import AddCandidateTool

    tool = _make_tool(backend)
    result = asyncio.run(tool.endpoint(
        content="test content",
        source="test-source",
        cluster_id="cluster-42",
        label="test label",
    ))

    assert result.startswith("Created CANDIDATE")
    assert "uuid=c1" in result
    assert "cluster_id=cluster-42" in result
    assert "evidence_strength=REPEATED" in result
    assert "distinct_sessions=3" in result
    assert "not recalled until approved" in result
    backend.create_candidate.assert_awaited_once()


@pytest.mark.unit
def test_refreshed_candidate() -> None:
    backend = MagicMock()
    backend.create_candidate = AsyncMock(return_value={
        "uuid": "c1",
        "scope": "CANDIDATE",
        "cluster_id": "cluster-42",
        "evidence_strength": "REPEATED",
        "distinct_sessions": 3,
        "created": False,
    })

    from menhir.mcp.tools.ingest.add_candidate import AddCandidateTool

    tool = _make_tool(backend)
    result = asyncio.run(tool.endpoint(
        content="test content",
        source="test-source",
        cluster_id="cluster-42",
        label="test label",
    ))

    assert result.startswith("Refreshed CANDIDATE")
    assert "uuid=c1" in result
    assert "cluster_id=cluster-42" in result
    assert "not recalled until approved" in result


@pytest.mark.unit
def test_backend_receives_all_arguments_by_keyword() -> None:
    backend = MagicMock()
    backend.create_candidate = AsyncMock(return_value={
        "uuid": "c1",
        "scope": "CANDIDATE",
        "cluster_id": "cluster-42",
        "evidence_strength": "STRONG",
        "distinct_sessions": 5,
        "created": True,
    })

    from menhir.mcp.tools.ingest.add_candidate import AddCandidateTool

    tool = _make_tool(backend)
    result = asyncio.run(tool.endpoint(
        content="important candidate",
        source="claude-code",
        cluster_id="cluster-42",
        label="important thing",
        kind="memory",
        candidate_type="friction",
        type="SEMANTIC",
        evidence_strength="STRONG",
        distinct_sessions=5,
        first_seen="2026-06-01",
        last_seen="2026-06-20",
        notes=["note 1", "note 2"],
        source_confidence=0.8,
    ))

    call_kwargs = backend.create_candidate.call_args.kwargs
    assert call_kwargs["content"] == "important candidate"
    assert call_kwargs["source"] == "claude-code"
    assert call_kwargs["cluster_id"] == "cluster-42"
    assert call_kwargs["label"] == "important thing"
    assert call_kwargs["kind"] == "memory"
    assert call_kwargs["candidate_type"] == "friction"
    assert call_kwargs["type"] == "SEMANTIC"
    assert call_kwargs["evidence_strength"] == "STRONG"
    assert call_kwargs["distinct_sessions"] == 5
    assert call_kwargs["first_seen"] == "2026-06-01"
    assert call_kwargs["last_seen"] == "2026-06-20"
    assert call_kwargs["notes"] == ["note 1", "note 2"]
    assert call_kwargs["source_confidence"] == 0.8


@pytest.mark.unit
def test_default_parameters() -> None:
    backend = MagicMock()
    backend.create_candidate = AsyncMock(return_value={
        "uuid": "c1",
        "scope": "CANDIDATE",
        "cluster_id": "cluster-42",
        "evidence_strength": "REPEATED",
        "distinct_sessions": 0,
        "created": True,
    })

    from menhir.mcp.tools.ingest.add_candidate import AddCandidateTool

    tool = _make_tool(backend)
    result = asyncio.run(tool.endpoint(
        content="test",
        source="test-source",
        cluster_id="cluster-42",
        label="test",
    ))

    call_kwargs = backend.create_candidate.call_args.kwargs
    assert call_kwargs["kind"] == "memory"
    assert call_kwargs["candidate_type"] == "other"
    assert call_kwargs["type"] == "SEMANTIC"
    assert call_kwargs["evidence_strength"] == "REPEATED"
    assert call_kwargs["distinct_sessions"] == 0
    assert call_kwargs["first_seen"] is None
    assert call_kwargs["last_seen"] is None
    assert call_kwargs["notes"] is None
    assert call_kwargs["source_confidence"] == 0.5
