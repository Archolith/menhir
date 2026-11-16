"""#669: every conflict/ and recall/ failure path is an MCP error result with a code."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from menhir.domain.recall import InvalidQueryPresetError, format_query_preset_values
from tests.mcp.error_contract import error_of, text_of

pytestmark = pytest.mark.asyncio

_GROUP = {
    "group_id": "grp-1",
    "members": [
        {"uuid": "u-1", "status": "unresolved"},
        {"uuid": "u-2", "status": "unresolved"},
    ],
}


def _tool(cls, backend):
    tool = cls()
    tool.get_backend = lambda: backend  # type: ignore[method-assign]
    return tool


def _conflict_backend(rows=None):
    backend = MagicMock()
    backend.list_conflict_groups = AsyncMock(return_value=[_GROUP] if rows is None else rows)
    backend.resolve_conflict_group = AsyncMock()
    return backend


async def _resolve(backend, **kwargs):
    from menhir.mcp.tools.conflict.resolve_conflict import ResolveConflictTool

    return await _tool(ResolveConflictTool, backend).execute(**kwargs)


# --- resolve_conflict -------------------------------------------------------


async def test_resolve_blank_group_id() -> None:
    out = await _resolve(_conflict_backend(), group_id="  ", action="keep_both")
    error_of(out, code="invalid_argument", tool="resolve_conflict")
    assert "Invalid group_id: value is required." in text_of(out)


async def test_resolve_invalid_action() -> None:
    out = await _resolve(_conflict_backend(), group_id="grp-1", action="nuke")
    error_of(out, code="invalid_argument")
    assert "Invalid action. Use: keep_both, replace, discard_new." in text_of(out)


async def test_resolve_unknown_group_is_not_found() -> None:
    out = await _resolve(_conflict_backend(rows=[]), group_id="grp-x", action="keep_both")
    error_of(out, code="not_found", tool="resolve_conflict")
    assert "Conflict group not found: grp-x" in text_of(out)


async def test_resolve_keep_both_with_uuids() -> None:
    out = await _resolve(_conflict_backend(), group_id="grp-1", action="keep_both", keep_uuid="u-1")
    error_of(out, code="invalid_argument")
    assert "action=keep_both must not include keep_uuid/remove_uuid." in text_of(out)


async def test_resolve_replace_requires_keep_uuid() -> None:
    out = await _resolve(_conflict_backend(), group_id="grp-1", action="replace")
    error_of(out, code="invalid_argument")
    assert "action=replace requires keep_uuid." in text_of(out)


async def test_resolve_keep_uuid_not_a_member() -> None:
    out = await _resolve(_conflict_backend(), group_id="grp-1", action="replace", keep_uuid="zz")
    error_of(out, code="invalid_argument")
    assert "keep_uuid zz is not a member of group_id=grp-1." in text_of(out)


async def test_resolve_remove_uuid_not_a_member() -> None:
    out = await _resolve(
        _conflict_backend(), group_id="grp-1", action="replace", keep_uuid="u-1", remove_uuid="zz"
    )
    error_of(out, code="invalid_argument")
    assert "remove_uuid zz is not a member of group_id=grp-1." in text_of(out)


async def test_resolve_remove_equals_keep() -> None:
    out = await _resolve(
        _conflict_backend(), group_id="grp-1", action="replace", keep_uuid="u-1", remove_uuid="u-1"
    )
    error_of(out, code="invalid_argument")
    assert "remove_uuid cannot equal keep_uuid." in text_of(out)


async def test_resolve_backend_value_error_keeps_its_message() -> None:
    backend = _conflict_backend()
    backend.resolve_conflict_group = AsyncMock(side_effect=ValueError("backend says no"))
    out = await _resolve(backend, group_id="grp-1", action="replace", keep_uuid="u-1")
    error_of(out, code="invalid_argument")
    assert "backend says no" in text_of(out)


# --- list_conflicts ---------------------------------------------------------


async def test_list_conflicts_invalid_status() -> None:
    from menhir.mcp.tools.conflict.list_conflicts import ListConflictsTool

    out = await _tool(ListConflictsTool, _conflict_backend()).execute(status="bad", limit=5)
    error_of(out, code="invalid_argument", tool="list_conflicts")
    assert "Invalid status. Use: unresolved, resolved, auto-resolved, all." in text_of(out)


async def test_list_conflicts_backend_value_error_keeps_its_message() -> None:
    from menhir.mcp.tools.conflict.list_conflicts import ListConflictsTool

    backend = _conflict_backend()
    backend.list_conflict_groups = AsyncMock(side_effect=ValueError("bad limit"))
    out = await _tool(ListConflictsTool, backend).execute(status="all", limit=5)
    error_of(out, code="invalid_argument")
    assert "bad limit" in text_of(out)


# --- recall presets ---------------------------------------------------------


def _preset_error() -> InvalidQueryPresetError:
    return InvalidQueryPresetError(f"Invalid preset 'brief'. Use: {format_query_preset_values()}.")


async def test_recall_memories_invalid_preset() -> None:
    from menhir.mcp.tools.recall.recall_memories import RecallMemoriesTool

    backend = MagicMock()
    backend.recall = AsyncMock(side_effect=_preset_error())
    out = await _tool(RecallMemoriesTool, backend).execute(query="q", preset="brief")
    error_of(out, code="invalid_argument", tool="recall_memories")
    assert f"Invalid preset 'brief'. Use: {format_query_preset_values()}." in text_of(out)


async def test_recall_memories_other_value_error_is_not_blamed_on_the_preset() -> None:
    from menhir.mcp.tools.recall.recall_memories import RecallMemoriesTool

    backend = MagicMock()
    backend.recall = AsyncMock(side_effect=ValueError("something else"))
    out = await _tool(RecallMemoriesTool, backend).execute(query="q")
    # Not a preset problem: it must not be reported as one (it is an internal error).
    assert "Invalid preset" not in text_of(out)


async def test_recall_context_invalid_preset() -> None:
    from menhir.mcp.tools.recall.recall_context_memories import RecallContextMemoriesTool

    backend = MagicMock()
    backend.fetch_flagged_memory_bootstrap_version = AsyncMock(return_value="v1")
    backend.recall = AsyncMock(side_effect=_preset_error())
    import menhir.mcp.tools.recall.recall_context_memories as mod

    original = mod._has_recent_flagged_bootstrap_read
    mod._has_recent_flagged_bootstrap_read = lambda *a, **k: True
    try:
        out = await _tool(RecallContextMemoriesTool, backend).execute(
            reader_id="bot", query="q", preset="brief"
        )
    finally:
        mod._has_recent_flagged_bootstrap_read = original
    error_of(out, code="invalid_argument", tool="recall_context_memories")
    assert "Invalid preset 'brief'" in text_of(out)


async def test_build_context_invalid_preset(monkeypatch) -> None:
    import menhir.mcp.tools.recall.build_context as mod
    from menhir.mcp.tools.recall.build_context import BuildContextTool

    monkeypatch.setattr(mod, "get_mcp_session", lambda: MagicMock(session_id="s"))
    backend = MagicMock()
    backend.build_context = AsyncMock(side_effect=_preset_error())
    out = await _tool(BuildContextTool, backend).execute(query="q", preset="brief")
    error_of(out, code="invalid_argument", tool="build_context")
    assert f"Invalid preset 'brief'. Use: {format_query_preset_values()}." in text_of(out)


# --- recall_context_memories bootstrap -------------------------------------


async def test_recall_context_bootstrap_required() -> None:
    from menhir.mcp.tools.recall.recall_context_memories import RecallContextMemoriesTool

    backend = MagicMock()
    backend.fetch_flagged_memory_bootstrap_version = AsyncMock(return_value="9:never-read")
    out = await _tool(RecallContextMemoriesTool, backend).execute(reader_id="never-read-bot")
    error_of(out, code="bootstrap_required", tool="recall_context_memories")
    text = text_of(out)
    assert "Context bootstrap required." in text
    assert "read_flagged_memories(reader_id='never-read-bot', workspace='')" in text
    assert "current_flagged_version=9:never-read" in text


# --- recall_history / recall_timeline --------------------------------------


async def test_recall_history_reports_the_real_exception_message() -> None:
    from menhir.mcp.tools.recall.recall_history import RecallHistoryTool

    backend = MagicMock()
    backend.recall_history = AsyncMock(side_effect=ValueError("query must not be blank"))
    out = await _tool(RecallHistoryTool, backend).execute(query="")
    error_of(out, code="invalid_argument", tool="recall_history")
    assert "query must not be blank" in text_of(out)


async def test_recall_timeline_value_error() -> None:
    from menhir.mcp.tools.recall.recall_timeline import RecallTimelineTool

    backend = MagicMock()
    backend.recall_timeline = AsyncMock(side_effect=ValueError("at and around are exclusive"))
    out = await _tool(RecallTimelineTool, backend).execute()
    error_of(out, code="invalid_argument", tool="recall_timeline")
    assert "at and around are exclusive" in text_of(out)


# --- query_structure --------------------------------------------------------


def _structure_tool(answers: dict, *, projects=None):
    from menhir.mcp.tools.recall.query_structure import QueryStructureTool

    known = (
        [{
            "name": "p1", "stack": "python", "description": "d", "root_path": "",
            "files_eligible": 10, "files_indexed": 10, "partial_index": False,
        }]
        if projects is None
        else projects
    )

    async def fake(project, query_type, params=None):
        if query_type == "projects":
            return known
        value = answers[query_type]
        if isinstance(value, Exception):
            raise value
        return value

    backend = MagicMock()
    backend.query_structure = AsyncMock(side_effect=fake)
    return _tool(QueryStructureTool, backend)


async def test_query_structure_project_required() -> None:
    out = await _structure_tool({}).execute(query_type="files")
    error_of(out, code="invalid_argument", tool="query_structure")
    assert text_of(out) == "project name is required for this query type."


async def test_query_structure_unknown_project_is_not_found() -> None:
    out = await _structure_tool({}).execute(query_type="files", project="ghost")
    error_of(out, code="not_found")
    assert "Project 'ghost' is not ingested in the structural graph." in text_of(out)


@pytest.mark.parametrize(
    ("query_type", "what"),
    [
        ("imports", "imports"),
        ("blast_radius", "blast_radius"),
        ("affected_tests", "affected_tests"),
        ("context", "context"),
    ],
)
async def test_query_structure_path_required(query_type: str, what: str) -> None:
    out = await _structure_tool({}).execute(query_type=query_type, project="p1")
    error_of(out, code="invalid_argument")
    assert text_of(out).startswith(f"path is required for {what} query")
    assert not text_of(out).startswith("Error:")


async def test_query_structure_unknown_query_type() -> None:
    out = await _structure_tool({}).execute(query_type="bogus", project="p1")
    error_of(out, code="invalid_argument")
    assert "Unknown query_type: bogus. Use one of: projects, overview" in text_of(out)


async def test_query_structure_dispatch_value_error() -> None:
    tool = _structure_tool({"overview": ValueError("dispatch exploded")})
    out = await tool.execute(query_type="overview", project="p1")
    error_of(out, code="invalid_argument")
    assert text_of(out) == "dispatch exploded"


@pytest.mark.parametrize(
    ("query_type", "answer", "what"),
    [
        ("imports", {"imports": [], "imported_by": []}, "imports"),
        ("tests", [], "test mappings"),
        ("symbols", {"symbols": [], "truncated": False}, "symbols"),
    ],
)
async def test_query_structure_unindexed_path(query_type: str, answer, what: str) -> None:
    tool = _structure_tool({query_type: answer, "context": {"error": "File not found"}})
    out = await tool.execute(query_type=query_type, project="p1", path="ghost.py")
    error_of(out, code="not_indexed")
    assert f"Cannot answer {what} for p1: not indexed" in text_of(out)


async def test_query_structure_blast_radius_unindexed() -> None:
    tool = _structure_tool({"blast_radius": {"unindexed_paths": ["ghost.py"], "coverage": {}}})
    out = await tool.execute(query_type="blast_radius", project="p1", path="ghost.py")
    error_of(out, code="not_indexed")
    assert "Cannot answer blast radius for p1: not indexed" in text_of(out)


async def test_query_structure_affected_tests_unindexed() -> None:
    data = {
        "changed_files": ["ghost.py"], "affected_source_files": [], "test_files": [],
        "test_command": "pytest", "unindexed_paths": ["ghost.py"], "coverage": {"known": True},
    }
    out = await _structure_tool({"affected_tests": data}).execute(
        query_type="affected_tests", project="p1", path="ghost.py"
    )
    error_of(out, code="not_indexed")
    assert "Cannot determine the affected tests:" in text_of(out)


async def test_query_structure_affected_tests_partial_index() -> None:
    data = {
        "changed_files": ["a.py"], "affected_source_files": [], "test_files": [],
        "test_command": "pytest", "unindexed_paths": [],
        "coverage": {"known": True, "partial_index": True, "files_indexed": 1, "files_eligible": 9},
    }
    out = await _structure_tool({"affected_tests": data}).execute(
        query_type="affected_tests", project="p1", path="a.py"
    )
    error_of(out, code="not_indexed")
    assert "this project is only partially indexed" in text_of(out)


async def test_query_structure_context_error() -> None:
    tool = _structure_tool({"context": {"error": "File not found in structure graph: x.py"}})
    out = await tool.execute(query_type="context", project="p1", path="x.py")
    error_of(out, code="invalid_argument")
    assert text_of(out) == "File not found in structure graph: x.py"
