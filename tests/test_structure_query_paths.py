"""#679 / #683: structure queries answer for the stored path spelling and say what they cut.

The failure these pin: `src\\a.py` or `./src/a.py` passed the coverage check (which normalized)
but missed the traversal (which did not), so blast_radius printed "Affected tests: none found
(project fully indexed)" -- a false all-clear that tells an agent to skip tests.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from menhir.domain.structure_paths import normalize_query_path, normalize_query_paths
from menhir.infrastructure.structure_queries import StructureGraphWriter

pytestmark = [pytest.mark.unit]

STORED = "src/a.py"
ROOT = "C:\\Repo"


# ---------------------------------------------------------------------------
# Pure normalization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("given", "root", "expected"),
    [
        ("./src/a.py", "", STORED),
        ("src\\a.py", "", STORED),
        (".\\src\\a.py", "", STORED),
        ("C:\\Repo\\src\\a.py", "c:/repo", STORED),
        ("c:/REPO/src/a.py", ROOT, STORED),
        ("/c/Repo/src/a.py", ROOT, STORED),
        ("/mnt/c/Repo/src/a.py", ROOT, STORED),
        ("/srv/repo/src/a.py", "/srv/repo", STORED),
        # Outside the root: left absolute, so it reports as not indexed rather than matching.
        ("D:\\Other\\src\\a.py", ROOT, "D:/Other/src/a.py"),
        ("C:\\Repository\\src\\a.py", ROOT, "C:/Repository/src/a.py"),
        # A plain leading slash outside any known root reads as repo-relative (#679 decision,
        # pinned by test_normalize_structure_path_matches_stored_spelling).
        ("/src/a.py", "", STORED),
        ("/src/a.py", "/srv/repo", STORED),
        ("/c/Repo/src/a.py", "", "/c/Repo/src/a.py"),
        # A one-letter top directory is a real directory, not a drive (#682's mangling).
        ("a/foo.py", "", "a/foo.py"),
    ],
)
def test_normalize_query_path(given: str, root: str, expected: str) -> None:
    assert normalize_query_path(given, root) == expected


def test_prefix_keeps_its_trailing_slash() -> None:
    assert normalize_query_path("src\\menhir\\", "", prefix=True) == "src/menhir/"
    assert normalize_query_path("./src/", "", prefix=True) == "src/"
    # Without prefix=True a trailing slash is not a directory marker.
    assert normalize_query_path("src/a.py/", "") == STORED


def test_normalize_query_paths_dedupes_and_reports_rewrites() -> None:
    paths, rewrites = normalize_query_paths(["src\\a.py", " ./src/a.py ", "", STORED, "b.py"])
    assert paths == [STORED, "b.py"]
    assert rewrites == {"src\\a.py": STORED, "./src/a.py": STORED}


# ---------------------------------------------------------------------------
# StructureGraphWriter: every Cypher call sees only stored spellings
# ---------------------------------------------------------------------------


class _RecordingNeo4j:
    def __init__(
        self,
        *,
        indexed: frozenset[str] = frozenset({STORED}),
        traversal: list[dict[str, Any]] | None = None,
        beyond: list[dict[str, Any]] | None = None,
        callers: int = 0,
        memories: int = 0,
    ) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.indexed = indexed
        self.traversal = traversal or []
        self.beyond = beyond or []
        self.callers = callers
        self.memories = memories

    def execute(self, query: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        params = dict(params or {})
        self.calls.append((query, params))
        if "RETURN n.root_path AS root_path" in query:
            return [{"root_path": ROOT}]
        if "RETURN DISTINCT n.structure_path AS path" in query:
            return [{"path": p} for p in params.get("paths", []) if p in self.indexed]
        if "UNWIND $layer" in query:
            # One reverse-import hop per call: serve the rows at this hop, plus a pass-through
            # node while deeper rows remain, so `traversal` still reads as hop -> importers.
            hop = sum(1 for q, _ in self.calls if "UNWIND $layer" in q)
            target = params["layer"][0]
            rows = [
                {"target": target, "importer_path": r["importer_path"], "importer_role": r.get("importer_role")}
                for r in self.traversal
                if r.get("hop_distance") == hop
            ]
            if not rows and any(r.get("hop_distance", 0) > hop for r in self.traversal):
                rows = [{"target": target, "importer_path": f"_hop{hop}.py", "importer_role": "file"}]
            return rows
        if "UNWIND $frontier" in query:
            return list(self.beyond)
        if "caller_sym.name AS caller_name" in query:
            return [
                {"caller_name": f"f{i}", "caller_class": None, "caller_file": f"c{i}.py",
                 "callee_name": "g", "callee_class": None}
                for i in range(self.callers)
            ][: params.get("limit", 10**6)]
        if "ANCHORED_TO" in query:
            return [
                {"uuid": f"m{i}", "name": f"m{i}", "preview": "", "linked_file": STORED,
                 "anchor_source": "", "last_accessed": i}
                for i in range(self.memories)
            ][: params.get("limit", 10**6)]
        return []

    def path_params(self) -> list[str]:
        """Every caller-derived path value handed to Cypher."""
        seen: list[str] = []
        for _query, params in self.calls:
            for key in ("paths", "f", "path", "file_path", "prefix", "frontier"):
                value = params.get(key)
                if isinstance(value, str):
                    seen.append(value)
                elif isinstance(value, list):
                    seen.extend(str(v) for v in value)
        return seen


SPELLINGS = ["./src/a.py", "src\\a.py", "C:\\Repo\\src\\a.py", "/c/Repo/src/a.py"]


@pytest.mark.parametrize("spelling", SPELLINGS)
def test_blast_radius_matches_the_stored_spelling(spelling: str) -> None:
    neo4j = _RecordingNeo4j()
    result = StructureGraphWriter(neo4j=neo4j).query_blast_radius("proj", [spelling])

    assert result["changed"] == [STORED]
    assert result["unindexed_paths"] == []
    assert set(neo4j.path_params()) == {STORED}, neo4j.path_params()


def test_blast_radius_dedupes_two_spellings_of_one_file() -> None:
    neo4j = _RecordingNeo4j()
    result = StructureGraphWriter(neo4j=neo4j).query_blast_radius("proj", ["src\\a.py", STORED])
    assert result["changed"] == [STORED]
    assert result["unindexed_paths"] == []


def test_blast_radius_reports_an_absolute_path_outside_the_root_as_unindexed() -> None:
    neo4j = _RecordingNeo4j()
    result = StructureGraphWriter(neo4j=neo4j).query_blast_radius("proj", ["D:\\Other\\src\\a.py"])
    assert result["unindexed_paths"] == ["D:/Other/src/a.py"]


@pytest.mark.parametrize("spelling", ["C:\\Elsewhere\\src\\a.py", "/d/Other/src/a.py"])
def test_coverage_check_uses_the_traversal_spelling(spelling: str) -> None:
    """Counterexample: the traversal and the coverage check must not normalize differently.
    A drive path outside the root stays absolute for the traversal, so the coverage check must
    not reduce it to `src/a.py`, find it, and call the input indexed -- that reproduces the
    "none found (project fully indexed)" all-clear."""
    neo4j = _RecordingNeo4j()
    result = StructureGraphWriter(neo4j=neo4j).query_blast_radius("proj", [spelling])
    assert result["unindexed_paths"] == [normalize_query_path(spelling, ROOT)]
    assert STORED not in neo4j.path_params()


def test_leading_slash_is_read_as_repo_relative_everywhere() -> None:
    neo4j = _RecordingNeo4j()
    result = StructureGraphWriter(neo4j=neo4j).query_blast_radius("proj", ["/src/a.py"])
    assert result["changed"] == [STORED] and result["unindexed_paths"] == []
    assert set(neo4j.path_params()) == {STORED}


def test_relative_paths_cost_no_root_lookup() -> None:
    neo4j = _RecordingNeo4j()
    StructureGraphWriter(neo4j=neo4j).query_blast_radius("proj", ["src\\a.py"])
    assert not any("RETURN n.root_path AS root_path" in q for q, _ in neo4j.calls)


@pytest.mark.parametrize("spelling", SPELLINGS)
def test_affected_tests_reports_the_stored_changed_files(spelling: str) -> None:
    neo4j = _RecordingNeo4j()
    result = StructureGraphWriter(neo4j=neo4j).query_affected_tests("proj", [spelling])
    assert result["changed_files"] == [STORED]
    assert set(neo4j.path_params()) == {STORED}


@pytest.mark.parametrize(
    ("method", "kwargs"),
    [
        ("query_imports", {"file_path": "src\\a.py"}),
        ("query_tests", {"file_path": "./src/a.py"}),
        ("query_context", {"path": "C:\\Repo\\src\\a.py"}),
        ("query_symbols", {"path": "/c/Repo/src/a.py"}),
    ],
)
def test_single_file_queries_match_the_stored_spelling(method: str, kwargs: dict) -> None:
    neo4j = _RecordingNeo4j()
    getattr(StructureGraphWriter(neo4j=neo4j), method)("proj", **kwargs)
    values = [v for v in neo4j.path_params() if v != STORED + "::"]
    assert values and set(values) == {STORED}, neo4j.path_params()


def test_prefix_queries_keep_the_directory_slash() -> None:
    neo4j = _RecordingNeo4j()
    writer = StructureGraphWriter(neo4j=neo4j)
    writer.query_files("proj", "src\\")
    writer.query_symbols("proj", ".\\src\\")
    assert set(neo4j.path_params()) == {"src/"}


# ---------------------------------------------------------------------------
# #683: caps are signalled
# ---------------------------------------------------------------------------


def test_depth_truncation_is_reported_when_importers_exist_beyond_the_limit() -> None:
    traversal = [{"importer_path": "deep.py", "hop_distance": 5}]
    cut = _RecordingNeo4j(traversal=traversal, beyond=[{"path": "deeper.py"}])
    result = StructureGraphWriter(neo4j=cut).query_blast_radius("proj", [STORED], max_depth=5)
    assert result["depth_truncated"] is True
    assert result["max_depth"] == 5

    whole = _RecordingNeo4j(traversal=traversal, beyond=[])
    assert StructureGraphWriter(neo4j=whole).query_blast_radius("proj", [STORED])["depth_truncated"] is False


def test_no_frontier_query_when_nothing_reached_the_limit() -> None:
    neo4j = _RecordingNeo4j(traversal=[{"importer_path": "b.py", "hop_distance": 1}])
    result = StructureGraphWriter(neo4j=neo4j).query_blast_radius("proj", [STORED])
    assert result["depth_truncated"] is False
    assert not any("UNWIND $frontier" in q for q, _ in neo4j.calls)


def test_function_callers_and_related_memories_report_their_caps() -> None:
    capped = StructureGraphWriter(neo4j=_RecordingNeo4j(callers=31, memories=11)).query_blast_radius(
        "proj", [STORED]
    )
    assert len(capped["function_callers"]) == 30 and capped["function_callers_truncated"] is True
    assert len(capped["related_memories"]) == 10 and capped["related_memories_truncated"] is True

    exact = StructureGraphWriter(neo4j=_RecordingNeo4j(callers=30, memories=10)).query_blast_radius(
        "proj", [STORED]
    )
    assert len(exact["function_callers"]) == 30 and exact["function_callers_truncated"] is False
    assert len(exact["related_memories"]) == 10 and exact["related_memories_truncated"] is False


# ---------------------------------------------------------------------------
# MCP tool output
# ---------------------------------------------------------------------------


def _projects() -> list[dict[str, Any]]:
    return [{
        "name": "p1", "stack": "python", "description": "d", "root_path": "",
        "files_eligible": 10, "files_indexed": 10, "partial_index": False,
    }]


def _tool(answers: dict[str, Any]):
    from menhir.mcp.tools.recall.query_structure import QueryStructureTool

    seen: list[tuple[str, dict | None]] = []

    async def fake(project, query_type, params=None):
        seen.append((query_type, params))
        if query_type == "projects":
            return _projects()
        return answers[query_type]

    backend = MagicMock()
    backend.query_structure = AsyncMock(side_effect=fake)
    tool = QueryStructureTool()
    tool.get_backend = MagicMock(return_value=backend)
    return tool, seen


def _run(tool, **kwargs) -> str:
    return asyncio.run(tool.endpoint(**kwargs))


def _blast(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "changed": [STORED], "directly_affected": [], "transitively_affected": [],
        "affected_tests": [], "cross_project_refs": [], "related_memories": [],
        "open_todos": [], "function_callers": [], "unindexed_paths": [],
        "coverage": {"known": True, "partial_index": False, "files_eligible": 10, "files_indexed": 10},
    }
    base.update(overrides)
    return base


def test_tool_echoes_the_rewritten_spelling_and_sends_the_stored_one() -> None:
    tool, seen = _tool({"blast_radius": _blast()})
    out = _run(tool, query_type="blast_radius", project="p1", path="src\\a.py")

    assert "read 'src\\a.py' as 'src/a.py'" in out
    assert ("blast_radius", {"file_paths": [STORED], "namespace": None}) in seen


@pytest.mark.parametrize(
    ("query_type", "answer", "what"),
    [
        ("imports", {"imports": [], "imported_by": []}, "imports"),
        ("tests", [], "test mappings"),
        ("symbols", {"symbols": [], "truncated": False}, "symbols"),
    ],
)
def test_unknown_exact_path_is_not_indexed_not_none_found(query_type: str, answer: Any, what: str) -> None:
    tool, _ = _tool({query_type: answer, "context": {"error": "File not found in structure graph: ghost.py"}})
    out = _run(tool, query_type=query_type, project="p1", path="ghost.py")

    assert f"Cannot answer {what} for p1: not indexed" in out
    assert "fully indexed" not in out


def test_known_file_with_no_imports_still_reports_none_found() -> None:
    tool, _ = _tool({"imports": {"imports": [], "imported_by": []}, "context": {"path": STORED}})
    out = _run(tool, query_type="imports", project="p1", path=STORED)
    assert "Imports: (none found) (project fully indexed)" in out


@pytest.mark.parametrize(
    ("query_type", "path", "answer"),
    [("files", "nope/", []), ("symbols", "nope/", {"symbols": [], "truncated": False})],
)
def test_empty_prefix_never_claims_full_index(query_type: str, path: str, answer: Any) -> None:
    tool, _ = _tool({query_type: answer})
    out = _run(tool, query_type=query_type, project="p1", path=path)
    assert "fully indexed" not in out
    assert "not in the index" in out


def test_blast_radius_renders_caps_and_open_todos() -> None:
    data = _blast(
        transitively_affected=["deep.py"], depth_truncated=True, max_depth=5,
        function_callers=[{"caller_file": "c.py", "caller": "f", "callee": "g"}],
        function_callers_truncated=True,
        related_memories=[{"name": "m", "preview": "p", "linked_file": STORED, "anchor_source": ""}],
        related_memories_truncated=True,
        open_todos=[{"uuid": "t1", "content": "fix a", "priority": "high", "file_path": STORED}],
    )
    tool, _ = _tool({"blast_radius": data})
    out = _run(tool, query_type="blast_radius", project="p1", path=STORED)

    assert "DEPTH LIMIT" in out and "5 hops" in out
    assert "Function-level callers (1) [first results only; more exist]" in out
    assert "Related memories (1) [most recent only; more exist" in out
    assert "Open TODOs at impacted files (1)" in out and "fix a" in out


def test_depth_limit_never_pairs_with_a_full_index_claim() -> None:
    tool, _ = _tool({"blast_radius": _blast(depth_truncated=True, max_depth=5)})
    blast = _run(tool, query_type="blast_radius", project="p1", path=STORED)
    assert "fully indexed" not in blast and "DEPTH LIMIT" in blast

    data = {
        "changed_files": [STORED], "affected_source_files": [], "test_files": [],
        "test_command": "pytest", "unindexed_paths": [], "coverage": {"known": True},
        "depth_truncated": True, "max_depth": 5,
    }
    tool, _ = _tool({"affected_tests": data})
    affected = _run(tool, query_type="affected_tests", project="p1", path=STORED)
    assert "fully indexed" not in affected and "5 import hops" in affected


def test_absolute_path_outside_root_gets_a_note() -> None:
    tool, _ = _tool({"blast_radius": _blast(unindexed_paths=["D:/Other/a.py"])})
    out = _run(tool, query_type="blast_radius", project="p1", path="D:\\Other\\a.py")
    assert "absolute path outside this project's recorded root" in out
    assert "not indexed" in out


def test_affected_tests_says_how_many_inputs_are_unindexed() -> None:
    data = {
        "changed_files": [STORED, "ghost.py"], "affected_source_files": [], "test_files": [],
        "test_command": "pytest", "unindexed_paths": ["ghost.py"], "coverage": {"known": True},
    }
    tool, _ = _tool({"affected_tests": data})
    out = _run(tool, query_type="affected_tests", project="p1", path=f"{STORED},ghost.py")
    assert "1 of the 2 changed files are not in the structure graph" in out


def test_affected_tests_names_unindexed_inputs_even_when_some_tests_were_found() -> None:
    data = {
        "changed_files": [STORED, "ghost.py"], "affected_source_files": [],
        "test_files": ["tests/test_a.py"], "test_command": "pytest tests/test_a.py",
        "unindexed_paths": ["ghost.py"], "coverage": {"known": True},
    }
    tool, _ = _tool({"affected_tests": data})
    out = _run(tool, query_type="affected_tests", project="p1", path=f"{STORED},ghost.py")

    assert "INCOMPLETE" in out and "? ghost.py" in out
    assert "tests/test_a.py" in out
