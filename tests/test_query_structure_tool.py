from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock


def _make_tool(backend: MagicMock):
    from menhir.mcp.tools.recall.query_structure import QueryStructureTool

    tool = QueryStructureTool()
    tool.get_backend = MagicMock(return_value=backend)
    return tool


def test_query_structure_reports_unknown_project_before_empty_results() -> None:
    backend = MagicMock()
    backend.query_structure = AsyncMock(
        return_value=[
            {
                "name": "cth.mcp.memory",
                "stack": "python",
                "description": "Core bot-facing docs for cth.mcp.memory.",
            }
        ]
    )
    tool = _make_tool(backend)

    result = asyncio.run(
        tool.endpoint(query_type="files", project="cth.context-engine", path="src/")
    )

    assert "Project 'cth.context-engine' is not ingested" in result
    assert "ingest_project" in result
    assert "No files found" not in result
    backend.query_structure.assert_awaited_once_with("", "projects")


def test_query_structure_projects_listing_still_works() -> None:
    backend = MagicMock()
    backend.query_structure = AsyncMock(
        return_value=[
            {
                "name": "cth.mcp.memory",
                "stack": "python",
                "description": "Core bot-facing docs for cth.mcp.memory.",
            }
        ]
    )
    tool = _make_tool(backend)

    result = asyncio.run(tool.endpoint(query_type="projects"))

    assert "Ingested projects:" in result
    assert "cth.mcp.memory (python)" in result


def test_query_structure_blast_radius_threads_namespace_without_nameerror() -> None:
    """Regression: `namespace` used to be read by the `blast_radius` branch inside
    `_dispatch` without ever being passed into `_dispatch`, raising a bare NameError
    on every call regardless of whether a namespace was supplied."""
    backend = MagicMock()
    projects_result = [{"name": "cth.mcp.memory", "stack": "python", "description": ""}]
    blast_radius_result = {
        "changed": ["a.py"],
        "directly_affected": [],
        "transitively_affected": [],
        "affected_tests": [],
        "cross_project_refs": [],
    }

    async def fake_query_structure(project, query_type, params=None):
        if query_type == "projects":
            return projects_result
        assert query_type == "blast_radius"
        assert params == {"file_paths": ["a.py"], "namespace": "myns"}
        return blast_radius_result

    backend.query_structure = AsyncMock(side_effect=fake_query_structure)
    tool = _make_tool(backend)

    result = asyncio.run(
        tool.endpoint(
            query_type="blast_radius",
            project="cth.mcp.memory",
            path="a.py",
            namespace="myns",
        )
    )

    assert "NameError" not in result
    assert "Blast radius for cth.mcp.memory:" in result


# ---------------------------------------------------------------------------
# Coverage-qualified negatives
#
# A structural query that finds nothing has two meanings: the thing genuinely has no
# dependents/tests, or it was never indexed. Reporting the second as the first is how a
# truncated index produces a false all-clear -- the exact failure this suite pins.
# ---------------------------------------------------------------------------

def _projects(**overrides):
    base = {
        "name": "p1", "stack": "python", "description": "d",
        "root_path": "", "files_eligible": 100, "files_indexed": 100,
        "partial_index": False,
    }
    base.update(overrides)
    return [base]


def _blast(**overrides):
    base = {
        "changed": ["a.py"], "directly_affected": [], "transitively_affected": [],
        "affected_tests": [], "cross_project_refs": [], "related_memories": [],
        "open_todos": [], "function_callers": [], "unindexed_paths": [],
        "coverage": {"known": True, "partial_index": False,
                     "files_eligible": 100, "files_indexed": 100},
    }
    base.update(overrides)
    return base


def _run(backend, **kwargs):
    return asyncio.run(_make_tool(backend).endpoint(**kwargs))


def _backend(projects_result, other_result):
    backend = MagicMock()

    async def fake(project, query_type, params=None):
        return projects_result if query_type == "projects" else other_result

    backend.query_structure = AsyncMock(side_effect=fake)
    return backend


def test_legacy_project_never_claims_full_index() -> None:
    """A project scanned before coverage tracking must NOT be reported as complete.

    Regression: the first implementation printed "(project fully indexed)" whenever
    partial_index was falsy, which is exactly the state of every pre-existing graph --
    turning an unknown into a false assurance.
    """
    legacy = _projects(files_eligible=None, files_indexed=None, partial_index=False)
    data = _blast(coverage={"known": False, "partial_index": False})
    out = _run(_backend(legacy, data), query_type="blast_radius", project="p1", path="a.py")

    assert "fully indexed" not in out
    assert "UNVERIFIED" in out or "unverified" in out


def test_partial_project_negative_is_qualified() -> None:
    partial = _projects(files_eligible=100, files_indexed=40, partial_index=True)
    data = _blast(coverage={"known": True, "partial_index": True,
                            "files_eligible": 100, "files_indexed": 40})
    out = _run(_backend(partial, data), query_type="blast_radius", project="p1", path="a.py")

    assert "not evidence of absence" in out
    assert "fully indexed" not in out


def test_fully_indexed_project_may_assert_absence() -> None:
    out = _run(_backend(_projects(), _blast()),
               query_type="blast_radius", project="p1", path="a.py")
    assert "fully indexed" in out


def test_unindexed_path_refuses_instead_of_reporting_zero() -> None:
    data = _blast(unindexed_paths=["ghost.py"])
    out = _run(_backend(_projects(), data),
               query_type="blast_radius", project="p1", path="ghost.py")

    assert "not indexed" in out.lower()
    assert "Total impact" not in out, "must not emit a zero-impact answer"


def test_endpoints_negative_is_qualified_when_coverage_unknown() -> None:
    legacy = _projects(files_eligible=None, files_indexed=None)
    out = _run(_backend(legacy, []), query_type="endpoints", project="p1")

    assert "No endpoints found" in out
    assert "not evidence of absence" in out


def test_files_negative_is_qualified_when_partial() -> None:
    partial = _projects(files_eligible=100, files_indexed=40, partial_index=True)
    out = _run(_backend(partial, []), query_type="files", project="p1")

    assert "not evidence of absence" in out
