from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest


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


def _snapshot(data, *, degraded=False, warning=""):
    return {
        "__snapshot_status__": {
            "project_id": "project-1",
            "view_key": "canonical",
            "snapshot_id": "snap-1",
            "generation": 4,
            "degraded": degraded,
            "warning": warning,
        },
        "data": data,
    }


@pytest.mark.parametrize(
    ("query_type", "path", "data", "legacy_text"),
    [
        (
            "overview",
            "",
            {
                "project": "p1",
                "stack": "python",
                "description": "snapshot",
                "entities": {},
                "edges": {},
                "coverage": {"known": True, "partial_index": False},
                "contains_repos": [],
            },
            "Project: p1",
        ),
        ("files", "", [{"path": "a.py", "role": "file", "description": ""}], "a.py"),
        ("imports", "a.py", {"imports": [], "imported_by": []}, "Import graph for a.py"),
        ("tests", "", [{"test": "test_a.py", "source": "a.py"}], "test_a.py"),
        ("endpoints", "", [{"name": "tool", "description": "d"}], "tool"),
        ("dependencies", "", ["neo4j"], "neo4j"),
        ("cross_refs", "", [], "No cross-project references"),
        (
            "blast_radius",
            "a.py",
            {
                "changed": ["a.py"],
                "directly_affected": [],
                "transitively_affected": [],
                "affected_tests": [],
                "cross_project_refs": [],
                "related_memories": [],
                "function_callers": [],
                "unindexed_paths": [],
                "coverage": {"known": True, "partial_index": False},
            },
            "Blast radius for p1",
        ),
        (
            "affected_tests",
            "a.py",
            {
                "changed_files": ["a.py"],
                "affected_source_files": [],
                "test_files": ["test_a.py"],
                "test_command": "pytest test_a.py",
                "unindexed_paths": [],
                "coverage": {"known": True, "partial_index": False},
            },
            "pytest test_a.py",
        ),
        ("symbols", "a.py", {"symbols": [], "truncated": False}, "No symbols found"),
        (
            "context",
            "a.py",
            {
                "path": "a.py",
                "summary": "A",
                "symbols": [],
                "truncated": False,
                "imports": [],
                "imported_by": [],
                "tested_by": [],
            },
            "Context for a.py",
        ),
    ],
)
def test_snapshot_envelope_is_centrally_unwrapped_for_every_renderer(
    query_type, path, data, legacy_text
) -> None:
    backend = MagicMock()

    async def fake(project, requested, params=None):
        if requested == "projects":
            return _projects()
        return _snapshot(data)

    backend.query_structure = AsyncMock(side_effect=fake)

    out = _run(backend, query_type=query_type, project="p1", path=path)

    assert out.splitlines()[0] == (
        "[SNAPSHOT id=snap-1 view=canonical generation=4 status=READY]"
    )
    assert legacy_text in out


def test_snapshot_only_project_bypasses_local_unknown_project_message() -> None:
    backend = MagicMock()

    async def fake(project, requested, params=None):
        if requested == "projects":
            return [{"name": "snapshot-only", "project_id": "project-1"}]
        return _snapshot([{"path": "a.py", "role": "file", "description": ""}])

    backend.query_structure = AsyncMock(side_effect=fake)

    out = _run(backend, query_type="files", project="snapshot-only")

    assert out.startswith("[SNAPSHOT id=snap-1")
    assert "not ingested" not in out
    assert "a.py" in out


def test_degraded_snapshot_header_and_warning_are_bounded() -> None:
    backend = MagicMock()

    async def fake(project, requested, params=None):
        if requested == "projects":
            return _projects()
        return _snapshot([], degraded=True, warning="x" * 500)

    backend.query_structure = AsyncMock(side_effect=fake)

    out = _run(backend, query_type="cross_refs", project="p1")
    lines = out.splitlines()

    assert lines[0] == "[SNAPSHOT id=snap-1 view=canonical generation=4 status=DEGRADED]"
    assert lines[1] == "[SNAPSHOT WARNING] " + ("x" * 240)


def test_plain_backend_output_remains_byte_identical() -> None:
    backend = _backend(_projects(), ["neo4j"])

    out = _run(backend, query_type="dependencies", project="p1")

    assert out == "Dependencies for p1 (1): neo4j"
