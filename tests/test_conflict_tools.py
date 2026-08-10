"""Unit tests for conflict MCP tools: list_conflicts, run_llm_conflict_review."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tool(cls, backend: MagicMock):
    tool = cls()
    tool.get_backend = MagicMock(return_value=backend)
    return tool


def _group_row(
    group_id: str = "g1",
    status: str = "unresolved",
    members: list[dict] | None = None,
) -> dict:
    return {
        "group_id": group_id,
        "created_at": "2026-03-29T12:00:00Z",
        "status": status,
        "members": members or [
            {"uuid": "aaa", "name": "Fact A", "content": "A is true", "status": "unresolved"},
            {"uuid": "bbb", "name": "Fact B", "content": "B is true", "status": "unresolved"},
        ],
    }


# ---------------------------------------------------------------------------
# ListConflictsTool
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_list_conflicts_empty() -> None:
    from menhir.mcp.tools.conflict.list_conflicts import ListConflictsTool

    backend = MagicMock()
    backend.list_conflict_groups = AsyncMock(return_value=[])
    tool = _make_tool(ListConflictsTool, backend)

    raw = asyncio.run(tool.endpoint(status="unresolved", limit=10))
    data = json.loads(raw)

    assert data["count"] == 0
    assert data["groups"] == []
    backend.list_conflict_groups.assert_awaited_once()


@pytest.mark.unit
def test_list_conflicts_returns_members_and_suggestion() -> None:
    from menhir.mcp.tools.conflict.list_conflicts import ListConflictsTool

    backend = MagicMock()
    backend.list_conflict_groups = AsyncMock(return_value=[_group_row()])
    tool = _make_tool(ListConflictsTool, backend)

    raw = asyncio.run(tool.endpoint(status="unresolved", limit=25))
    data = json.loads(raw)

    assert data["count"] == 1
    group = data["groups"][0]
    assert group["group_id"] == "g1"
    assert group["member_count"] == 2
    assert group["members"][0]["tag"] == "older"
    assert group["members"][1]["tag"] == "newer"
    assert "suggested_resolution" in group
    assert group["suggested_resolution"]["action"] == "replace"


@pytest.mark.unit
def test_list_conflicts_status_all() -> None:
    from menhir.mcp.tools.conflict.list_conflicts import ListConflictsTool

    backend = MagicMock()
    backend.list_conflict_groups = AsyncMock(return_value=[
        _group_row("g1", "unresolved"),
        _group_row("g2", "resolved"),
    ])
    tool = _make_tool(ListConflictsTool, backend)

    raw = asyncio.run(tool.endpoint(status="all", limit=50))
    data = json.loads(raw)

    assert data["count"] == 2
    # When status=all, the filter should be None
    call_kwargs = backend.list_conflict_groups.call_args.kwargs
    assert call_kwargs["status"] is None


@pytest.mark.unit
def test_list_conflicts_single_member_no_suggestion() -> None:
    from menhir.mcp.tools.conflict.list_conflicts import ListConflictsTool

    single_member_row = _group_row(members=[{"uuid": "aaa", "name": "X", "content": "x", "status": "unresolved"}])
    backend = MagicMock()
    backend.list_conflict_groups = AsyncMock(return_value=[single_member_row])
    tool = _make_tool(ListConflictsTool, backend)

    raw = asyncio.run(tool.endpoint())
    data = json.loads(raw)

    group = data["groups"][0]
    assert group["member_count"] == 1
    assert "suggested_resolution" not in group


# ---------------------------------------------------------------------------
# RunLLMReviewTool
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_run_llm_review_returns_counts() -> None:
    from menhir.mcp.tools.conflict.run_llm_review import RunLLMReviewTool

    backend = MagicMock()
    backend.confirm_pending_conflicts = AsyncMock(return_value={
        "processed": 5,
        "confirmed": 3,
        "cleared": 1,
        "errors": 1,
    })
    tool = _make_tool(RunLLMReviewTool, backend)

    raw = asyncio.run(tool.endpoint(limit=10))
    data = json.loads(raw)

    assert data["processed"] == 5
    assert data["confirmed"] == 3
    assert data["cleared"] == 1
    backend.confirm_pending_conflicts.assert_awaited_once_with(limit=10, verbose=True)


@pytest.mark.unit
def test_run_llm_review_default_limit() -> None:
    from menhir.mcp.tools.conflict.run_llm_review import RunLLMReviewTool

    backend = MagicMock()
    backend.confirm_pending_conflicts = AsyncMock(return_value={"processed": 0})
    tool = _make_tool(RunLLMReviewTool, backend)

    asyncio.run(tool.endpoint())

    backend.confirm_pending_conflicts.assert_awaited_once_with(limit=20, verbose=True)


@pytest.mark.unit
def test_run_llm_review_zero_pending() -> None:
    from menhir.mcp.tools.conflict.run_llm_review import RunLLMReviewTool

    backend = MagicMock()
    backend.confirm_pending_conflicts = AsyncMock(return_value={
        "processed": 0,
        "confirmed": 0,
        "cleared": 0,
        "errors": 0,
    })
    tool = _make_tool(RunLLMReviewTool, backend)

    raw = asyncio.run(tool.endpoint(limit=5))
    data = json.loads(raw)

    assert data["processed"] == 0
