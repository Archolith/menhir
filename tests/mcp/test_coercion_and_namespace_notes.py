"""#670: no silent coercion at the MCP boundary. #671: reads of an empty namespace say why."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from menhir.infrastructure import todo_repository
from menhir.mcp.tools.ingest.add_memory import AddMemoryTool
from menhir.mcp.tools.ops.add_todo import TODO_PRIORITIES, AddTodoTool
from menhir.mcp.tools.ops.list_namespace_memories import ListNamespaceMemoriesTool
from menhir.mcp.tools.ops.list_todos import TODO_STATUSES, ListTodosTool
from menhir.mcp.tools.recall import query_structure
from menhir.mcp.tools.recall.recall_history import RecallHistoryTool
from menhir.mcp.tools.recall.recall_memories import RecallMemoriesTool
from tests.mcp.error_contract import error_of, text_of

pytestmark = [pytest.mark.unit]


def _tool(cls, backend):
    tool = cls()
    tool.get_backend = lambda: backend  # type: ignore[method-assign]
    return tool


# --- #670: enums -------------------------------------------------------------------------


def test_the_mcp_enum_sets_match_what_the_repository_stores() -> None:
    assert set(TODO_PRIORITIES) == todo_repository._VALID_PRIORITIES
    assert set(TODO_STATUSES) == todo_repository._VALID_STATUSES


@pytest.mark.asyncio
async def test_add_todo_rejects_an_unknown_priority_instead_of_storing_normal() -> None:
    backend = MagicMock()
    out = await _tool(AddTodoTool, backend).execute(text="t", priority="urgent")
    error = error_of(out, code="invalid_argument")
    assert "'urgent'" in error["message"] and "low, normal, high" in error["message"]
    assert not backend.mock_calls


@pytest.mark.asyncio
async def test_list_todos_rejects_an_unknown_status_instead_of_listing_open() -> None:
    backend = MagicMock()
    out = await _tool(ListTodosTool, backend).execute(status="bogus")
    error = error_of(out, code="invalid_argument")
    assert "open, closed" in error["message"]
    assert not backend.mock_calls


@pytest.mark.asyncio
async def test_add_memory_temporal_without_valid_at_is_an_error_not_an_ordinary_memory() -> None:
    backend = MagicMock()
    for spelling in ("TEMPORAL", "temporal"):
        out = await _tool(AddMemoryTool, backend).execute(text="dentist", type=spelling)
        error = error_of(out, code="invalid_argument")
        assert "valid_at" in error["message"]
    assert not backend.mock_calls


# --- #670: limits ------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(("asked", "applied"), [(500, 200), (0, 1), (-3, 1)])
async def test_list_todos_says_when_it_clamped_the_limit(asked, applied) -> None:
    backend = MagicMock(list_todos=AsyncMock(return_value=[]))
    out = await _tool(ListTodosTool, backend).endpoint(limit=asked)
    assert backend.list_todos.await_args.kwargs["limit"] == applied
    assert f"(limit {applied} applied; allowed 1..200)" in out


@pytest.mark.asyncio
async def test_list_todos_says_nothing_about_a_limit_it_honoured() -> None:
    backend = MagicMock(list_todos=AsyncMock(return_value=[]))
    out = await _tool(ListTodosTool, backend).endpoint(limit=25)
    assert backend.list_todos.await_args.kwargs["limit"] == 25
    assert "applied" not in out


def _history_backend(memories: list[dict] | None = None, status: dict | None = None) -> MagicMock:
    return MagicMock(
        recall_history=AsyncMock(return_value={"query": "q", "memories": memories or []}),
        get_namespace_status=AsyncMock(return_value=status or {"known": True, "similar": []}),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(("asked", "applied"), [(80, 50), (0, 1)])
async def test_recall_history_echoes_the_limit_it_applied(asked, applied) -> None:
    backend = _history_backend()
    payload = json.loads(await _tool(RecallHistoryTool, backend).endpoint("q", limit=asked))
    assert backend.recall_history.await_args.kwargs["limit"] == applied
    assert payload["limit_applied"] == applied


@pytest.mark.asyncio
async def test_recall_history_has_no_limit_applied_when_the_limit_was_honoured() -> None:
    payload = json.loads(await _tool(RecallHistoryTool, _history_backend()).endpoint("q", limit=30))
    assert "limit_applied" not in payload


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, -1])
async def test_recall_memories_rejects_a_limit_below_one(limit) -> None:
    backend = MagicMock()
    out = await _tool(RecallMemoriesTool, backend).execute(query="q", limit=limit)
    error_of(out, code="invalid_argument")
    assert not backend.mock_calls


def test_query_structure_documents_the_documents_query_type() -> None:
    assert '"documents"' in (query_structure.query_structure.__doc__ or "")
    assert "documents" in (query_structure.QueryStructureTool.endpoint.__doc__ or "")


# --- #671: namespace notes on reads ------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("namespace", ["PFCC", "pfcc "])
async def test_recall_history_on_an_unknown_spelling_carries_a_note(namespace) -> None:
    backend = _history_backend(status={"known": False, "similar": ["pfcc"]})
    payload = json.loads(
        await _tool(RecallHistoryTool, backend).endpoint("q", namespace=namespace)
    )
    assert payload["memories"] == []
    assert "Did you mean 'pfcc'?" in payload["namespace_note"]


@pytest.mark.asyncio
@pytest.mark.parametrize("namespace", ["", "default"])
async def test_recall_history_on_the_default_namespace_gets_no_note_or_probe(namespace) -> None:
    backend = _history_backend(status={"known": False, "similar": []})
    payload = json.loads(
        await _tool(RecallHistoryTool, backend).endpoint("q", namespace=namespace)
    )
    assert "namespace_note" not in payload
    backend.get_namespace_status.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_non_empty_recall_is_never_probed() -> None:
    backend = _history_backend(
        memories=[{"content": "c", "uuid": "u"}], status={"known": False, "similar": []}
    )
    payload = json.loads(await _tool(RecallHistoryTool, backend).endpoint("q", namespace="x"))
    assert "namespace_note" not in payload
    backend.get_namespace_status.assert_not_awaited()


@pytest.mark.asyncio
async def test_recall_memories_on_an_unknown_namespace_carries_a_note() -> None:
    backend = MagicMock(
        recall=AsyncMock(return_value={"results": [], "preset": "knowledge"}),
        get_namespace_status=AsyncMock(return_value={"known": False, "similar": ["pfcc"]}),
    )
    out = await _tool(RecallMemoriesTool, backend).endpoint("q", namespace="PFCC")
    payload = json.loads(text_of(out))
    assert payload["items"] == []
    assert "not evidence of no history" in payload["namespace_note"]


# --- #671: one default spelling ----------------------------------------------------------


@pytest.mark.asyncio
async def test_list_namespace_memories_reads_an_empty_namespace_as_default() -> None:
    backend = MagicMock(list_namespace_records=AsyncMock(return_value={"items": []}))
    await _tool(ListNamespaceMemoriesTool, backend).endpoint()
    assert backend.list_namespace_records.await_args.args[0] == "default"
