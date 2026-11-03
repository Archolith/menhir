"""Contract tests for the read-only review_inbox MCP tool."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from menhir.mcp.contracts import assert_tool_scopes_declared, validate_tool_metadata
from menhir.mcp.tools import registered_tools
from menhir.mcp.tools.ops.review_inbox import ReviewInboxTool, review_inbox


def _backend(
    *,
    conflicts=None,
    failed=None,
    candidates=None,
    todos=None,
    questions=None,
) -> MagicMock:
    backend = MagicMock()
    backend.list_conflict_groups = AsyncMock(return_value=conflicts or [])
    backend.list_episode_processing = AsyncMock(return_value=failed or [])
    backend.list_candidates = AsyncMock(return_value=candidates or [])
    backend.list_todos = AsyncMock(return_value=todos or [])
    backend.list_artifact_questions = AsyncMock(return_value=questions or [])
    return backend


def _tool(backend: MagicMock) -> ReviewInboxTool:
    tool = ReviewInboxTool()
    tool.get_backend = MagicMock(return_value=backend)
    return tool


def _full_backend() -> MagicMock:
    return _backend(
        conflicts=[{
            "group_id": "g-1",
            "members": [{"uuid": "m1", "name": "Uses Postgres"}, {"uuid": "m2", "name": "Uses MySQL"}],
        }],
        failed=[{"uuid": "ep-1", "processing_error": "llm timeout"}],
        candidates=[{"uuid": "c-1", "label": "Prefers tabs"}],
        todos=[
            {"uuid": "t-1", "content": "old thing", "stale": True, "age_days": 40},
            {"uuid": "t-2", "content": "fresh thing", "stale": False},
        ],
        questions=[{
            "question_uuid": "q-1", "text": "Which store?", "artifact_title": "Plan A",
            "artifact_uuid": "a-1", "ordinal": 0,
        }],
    )


@pytest.mark.asyncio
async def test_all_sections_render_with_items_and_acting_tools() -> None:
    out = await _tool(_full_backend()).endpoint()
    assert out.splitlines()[0] == "Review inbox (all namespaces)"
    assert "Conflicts (1):" in out and "group_id=g-1" in out and "Uses Postgres" in out
    assert "resolve_conflict" in out
    assert "Failed enrichments (1):" in out and "uuid=ep-1" in out and "llm timeout" in out
    assert "get_enrichment_status" in out and "force_reenrich" in out
    assert "Candidates awaiting review (1):" in out and "uuid=c-1" in out
    assert "#431" in out and "explorer" in out
    assert "Stale TODOs (1" in out and "TODO" in out and "uuid=t-1" in out
    assert "resolve_todo, close_todo, supersede_todo" in out
    assert "Open artifact questions (1):" in out and "question_uuid=q-1" in out and "Plan A" in out
    assert "get_artifact" in out
    assert out.count("Act with:") == 5
    print(out)


@pytest.mark.asyncio
async def test_empty_inbox() -> None:
    out = await _tool(_backend()).endpoint()
    assert out == "Review inbox (all namespaces)\nNothing is waiting on a decision."


@pytest.mark.asyncio
async def test_empty_sections_print_none() -> None:
    out = await _tool(_backend(failed=[{"uuid": "ep-1"}])).endpoint()
    assert "Conflicts: none" in out
    assert "Candidates: none" in out
    assert "Stale TODOs: none" in out
    assert "Open artifact questions: none" in out


@pytest.mark.asyncio
async def test_cap_shows_plus_and_truncates() -> None:
    rows = [{"uuid": f"c-{i}", "label": f"cand {i}"} for i in range(3)]
    out = await _tool(_backend(candidates=rows)).endpoint(per_category=2)
    assert "Candidates awaiting review (2+):" in out
    assert out.count("uuid=c-") == 2
    # the fetch asked for one more than the cap
    assert "uuid=c-2" not in out


@pytest.mark.asyncio
async def test_fetches_cap_plus_one() -> None:
    backend = _backend()
    await _tool(backend).endpoint(per_category=3)
    assert backend.list_conflict_groups.await_args.kwargs["limit"] == 4
    assert backend.list_episode_processing.await_args.kwargs["limit"] == 4
    assert backend.list_candidates.await_args.kwargs["limit"] == 4
    assert backend.list_artifact_questions.await_args.kwargs["limit"] == 4


@pytest.mark.asyncio
async def test_stale_filter_excludes_non_stale() -> None:
    todos = [
        {"uuid": "fresh-1", "content": "x", "stale": False},
        {"uuid": "stale-1", "content": "y", "stale": True},
        {"uuid": "fresh-2", "content": "z"},
    ]
    out = await _tool(_backend(todos=todos)).endpoint()
    assert "uuid=stale-1" in out
    assert "fresh-1" not in out and "fresh-2" not in out
    assert "Stale TODOs (1," in out


@pytest.mark.asyncio
async def test_all_non_stale_todos_means_none() -> None:
    out = await _tool(_backend(todos=[{"uuid": "a", "stale": False}])).endpoint()
    assert "Nothing is waiting on a decision." in out


@pytest.mark.asyncio
async def test_stale_plus_only_when_page_full() -> None:
    todos = [{"uuid": f"t-{i}", "content": "x", "stale": True} for i in range(200)]
    out = await _tool(_backend(todos=todos)).endpoint(per_category=2)
    assert "Stale TODOs (200+," in out
    assert out.count("uuid=t-") == 2


@pytest.mark.asyncio
async def test_failing_read_is_isolated_and_leaks_no_message() -> None:
    backend = _full_backend()
    backend.list_conflict_groups = AsyncMock(side_effect=ValueError("secret content here"))
    out = await _tool(backend).endpoint()
    assert "Conflicts: unavailable (ValueError)" in out
    assert "secret content here" not in out
    assert "Failed enrichments (1):" in out
    assert "Open artifact questions (1):" in out


@pytest.mark.asyncio
async def test_namespace_reaches_scoped_reads_and_candidates_are_skipped() -> None:
    backend = _full_backend()
    out = await _tool(backend).endpoint(namespace="tenant-a")
    assert out.splitlines()[0] == "Review inbox (tenant-a)"
    assert backend.list_conflict_groups.await_args.kwargs["namespace"] == "tenant-a"
    assert backend.list_episode_processing.await_args.kwargs["namespace"] == "tenant-a"
    assert backend.list_todos.await_args.kwargs["namespace"] == "tenant-a"
    assert backend.list_artifact_questions.await_args.kwargs["namespace"] == "tenant-a"
    # list_candidates takes no namespace; a scoped call must not read other silos' candidates.
    backend.list_candidates.assert_not_awaited()
    assert "uuid=c-1" not in out
    assert "not namespace-scoped" in out


@pytest.mark.asyncio
async def test_no_namespace_passes_none_and_reads_candidates() -> None:
    backend = _backend()
    await _tool(backend).endpoint()
    assert backend.list_todos.await_args.kwargs["namespace"] is None
    assert backend.list_artifact_questions.await_args.kwargs["namespace"] is None
    assert "namespace" not in backend.list_conflict_groups.await_args.kwargs
    backend.list_candidates.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("requested,expected_limit", [(0, 2), (-5, 2), (100, 26), (5, 6)])
async def test_per_category_is_clamped(requested: int, expected_limit: int) -> None:
    backend = _backend()
    await _tool(backend).endpoint(per_category=requested)
    assert backend.list_artifact_questions.await_args.kwargs["limit"] == expected_limit


@pytest.mark.asyncio
async def test_module_wrapper_delegates(monkeypatch) -> None:
    backend = _backend()
    monkeypatch.setattr(ReviewInboxTool, "get_backend", lambda self: backend)
    out = await review_inbox()
    assert "Nothing is waiting on a decision." in out


def test_tool_metadata_and_registration() -> None:
    assert ReviewInboxTool in registered_tools()
    assert ReviewInboxTool.read_only_hint is True and ReviewInboxTool.destructive_hint is False
    assert_tool_scopes_declared([ReviewInboxTool])
    validate_tool_metadata([ReviewInboxTool])
    assert_tool_scopes_declared(registered_tools())
    validate_tool_metadata(registered_tools())
