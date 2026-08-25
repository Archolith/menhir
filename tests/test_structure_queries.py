"""Tests for infrastructure.structure_queries — Cypher writer for structural entities."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from menhir.infrastructure.project_scanner import (
    CrossProjectRef,
    DirEntry,
    EndpointEntry,
    FileEntry,
    ImportEdge,
    ProjectScanResult,
    SymbolEntry,
    TestEdge,
)
from menhir.infrastructure.structure_queries import StructureGraphWriter


# ---------------------------------------------------------------------------
# Stub Neo4j
# ---------------------------------------------------------------------------

class RecordingNeo4j:
    """Records all Cypher calls for assertion."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def execute(self, query: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        self.calls.append((query, params or {}))
        # For count-returning queries, return a plausible result
        if "count(r)" in query:
            return [{"cnt": 1}]
        if "scan_fingerprint" in query and "RETURN" in query:
            return [{"fp": None}]
        return []


def _make_scan(**overrides: Any) -> ProjectScanResult:
    defaults: dict[str, Any] = dict(
        name="test-project",
        root_path="/tmp/test-project",
        stack="python",
        description="A test project",
        directories=[DirEntry(rel_path="src"), DirEntry(rel_path="tests")],
        files=[
            FileEntry(rel_path="src/main.py", role="entrypoint"),
            FileEntry(rel_path="src/service.py", role="file"),
            FileEntry(rel_path="tests/test_service.py", role="test"),
        ],
        dependencies=["requests", "pydantic"],
        imports=[ImportEdge(source_path="src/service.py", target_path="src/main.py")],
        test_edges=[TestEdge(test_path="tests/test_service.py", source_path="src/service.py")],
        endpoints=[EndpointEntry(name="GET /health", file_path="src/main.py", kind="http_route")],
        cross_project_refs=[CrossProjectRef(target_project="other-proj", mechanism="http", evidence="port 8080 in .env")],
        scan_fingerprint="abc123",
    )
    defaults.update(overrides)
    return ProjectScanResult(**defaults)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestStructureGraphWriter:
    def test_write_returns_counts(self):
        neo4j = RecordingNeo4j()
        writer = StructureGraphWriter(neo4j=neo4j)
        scan = _make_scan()

        result = writer.write_project(scan, session_id="s1", user_id="u1")

        assert result["entities"] > 0
        assert result["edges"] > 0

    def test_project_entity_created(self):
        neo4j = RecordingNeo4j()
        writer = StructureGraphWriter(neo4j=neo4j)
        scan = _make_scan()

        writer.write_project(scan, session_id="s1", user_id="u1")

        # Find the first call that contains MERGE (project entity MERGE)
        first_merge = next((q, p) for q, p in neo4j.calls if "MERGE" in q)
        first_query, first_params = first_merge
        assert "MERGE" in first_query
        assert first_params["sp"] == "test-project"
        assert first_params["spath"] == "."

    def test_directory_entities_batched(self):
        neo4j = RecordingNeo4j()
        writer = StructureGraphWriter(neo4j=neo4j)
        scan = _make_scan()

        writer.write_project(scan, session_id="s1", user_id="u1")

        # Find the UNWIND query for directories
        dir_calls = [
            (q, p) for q, p in neo4j.calls
            if "UNWIND" in q and "rows" in p and p["rows"]
            and any(r.get("structure_role") == "directory" for r in p["rows"])
        ]
        assert len(dir_calls) == 1
        assert len(dir_calls[0][1]["rows"]) == 2  # src, tests

    def test_file_entities_batched(self):
        neo4j = RecordingNeo4j()
        writer = StructureGraphWriter(neo4j=neo4j)
        scan = _make_scan()

        writer.write_project(scan, session_id="s1", user_id="u1")

        file_calls = [
            (q, p) for q, p in neo4j.calls
            if "UNWIND" in q and "rows" in p and p["rows"]
            and any(r.get("structure_role") in ("entrypoint", "file", "test") for r in p["rows"])
        ]
        assert len(file_calls) == 1
        assert len(file_calls[0][1]["rows"]) == 3

    def test_dependency_entities(self):
        neo4j = RecordingNeo4j()
        writer = StructureGraphWriter(neo4j=neo4j)
        scan = _make_scan()

        writer.write_project(scan, session_id="s1", user_id="u1")

        dep_calls = [
            (q, p) for q, p in neo4j.calls
            if "UNWIND" in q and "rows" in p and p["rows"]
            and any(r.get("structure_role") == "dependency" for r in p["rows"])
        ]
        assert len(dep_calls) == 1
        dep_names = {r["name"] for r in dep_calls[0][1]["rows"]}
        assert dep_names == {"requests", "pydantic"}

    def test_endpoint_entities(self):
        neo4j = RecordingNeo4j()
        writer = StructureGraphWriter(neo4j=neo4j)
        scan = _make_scan()

        writer.write_project(scan, session_id="s1", user_id="u1")

        ep_calls = [
            (q, p) for q, p in neo4j.calls
            if "UNWIND" in q and "rows" in p and p["rows"]
            and any(r.get("structure_role") == "endpoint" for r in p["rows"])
        ]
        assert len(ep_calls) == 1
        assert ep_calls[0][1]["rows"][0]["name"] == "GET /health"

    def test_every_structural_batch_row_carries_the_settled_project_id(self):
        """The symbol sub-writer once escaped the shared claim with a NULL id on every row."""
        neo4j = RecordingNeo4j()
        writer = StructureGraphWriter(neo4j=neo4j)
        scan = _make_scan(
            project_id="project-id-1",
            symbols=[
                SymbolEntry(
                    file_path="src/service.py",
                    name="serve",
                    kind="function",
                    line_no=10,
                    signature="def serve()",
                    docstring="",
                    parent="",
                )
            ],
        )

        writer.write_project(scan, session_id="s1", user_id="u1")

        batches = [
            params["rows"]
            for _, params in neo4j.calls
            if params.get("rows")
            and any(row.get("structure_role") is not None for row in params["rows"])
        ]
        assert batches
        assert {
            row["structure_role"]
            for rows in batches
            for row in rows
        } >= {"directory", "entrypoint", "dependency", "endpoint", "symbol"}
        assert all(
            row.get("structure_project_id") == "project-id-1"
            for rows in batches
            for row in rows
        )

    def test_contains_edges(self):
        neo4j = RecordingNeo4j()
        writer = StructureGraphWriter(neo4j=neo4j)
        scan = _make_scan()

        writer.write_project(scan, session_id="s1", user_id="u1")

        contains_calls = [
            (q, p) for q, p in neo4j.calls
            if "CONTAINS" in q and "edges" in p
        ]
        assert len(contains_calls) == 1
        edges = contains_calls[0][1]["edges"]
        # Should have dir containment + file containment edges
        assert len(edges) >= 4  # 2 dirs + at least 2 files with parents

    def test_depends_on_edges(self):
        neo4j = RecordingNeo4j()
        writer = StructureGraphWriter(neo4j=neo4j)
        scan = _make_scan()

        writer.write_project(scan, session_id="s1", user_id="u1")

        dep_calls = [(q, p) for q, p in neo4j.calls if "DEPENDS_ON" in q and "edges" in p]
        assert len(dep_calls) == 1
        assert len(dep_calls[0][1]["edges"]) == 2

    def test_tests_edges(self):
        neo4j = RecordingNeo4j()
        writer = StructureGraphWriter(neo4j=neo4j)
        scan = _make_scan()

        writer.write_project(scan, session_id="s1", user_id="u1")

        test_calls = [(q, p) for q, p in neo4j.calls if "TESTS" in q and "edges" in p]
        assert len(test_calls) == 1
        assert test_calls[0][1]["edges"][0]["source_path"] == "tests/test_service.py"

    def test_imports_edges(self):
        neo4j = RecordingNeo4j()
        writer = StructureGraphWriter(neo4j=neo4j)
        scan = _make_scan()

        writer.write_project(scan, session_id="s1", user_id="u1")

        import_calls = [(q, p) for q, p in neo4j.calls if "IMPORTS" in q and "edges" in p]
        assert len(import_calls) == 1
        assert import_calls[0][1]["edges"][0]["source_path"] == "src/service.py"

    def test_exposes_edges(self):
        neo4j = RecordingNeo4j()
        writer = StructureGraphWriter(neo4j=neo4j)
        scan = _make_scan()

        writer.write_project(scan, session_id="s1", user_id="u1")

        exposes_calls = [(q, p) for q, p in neo4j.calls if "EXPOSES" in q and "edges" in p]
        assert len(exposes_calls) == 1
        assert exposes_calls[0][1]["edges"][0]["target_path"] == "endpoint:GET /health"

    def test_query_linked_memories_orders_by_aggregated_last_accessed(self):
        class _QueryNeo4j(RecordingNeo4j):
            def execute(self, query: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
                self.calls.append((query, params or {}))
                return [
                    {
                        "uuid": "mem-1",
                        "name": "Memory 1",
                        "preview": "preview",
                        "linked_file": "src/main.py",
                        "anchor_source": "import",
                        "last_accessed": "2026-04-01T00:00:00+00:00",
                    }
                ]

        neo4j = _QueryNeo4j()
        writer = StructureGraphWriter(neo4j=neo4j)

        result = writer.query_linked_memories("test-project", ["src/main.py"], limit=5)

        assert result == [
            {
                "uuid": "mem-1",
                "name": "Memory 1",
                "preview": "preview",
                "linked_file": "src/main.py",
                "anchor_source": "import",
            }
        ]
        query, params = neo4j.calls[0]
        assert "max(sem.last_accessed) AS last_accessed" in query
        assert "ORDER BY last_accessed DESC" in query
        assert params == {"p": "test-project", "paths": ["src/main.py"], "limit": 5}

    def test_calls_edges(self):
        neo4j = RecordingNeo4j()
        writer = StructureGraphWriter(neo4j=neo4j)
        scan = _make_scan()

        writer.write_project(scan, session_id="s1", user_id="u1")

        calls_calls = [(q, p) for q, p in neo4j.calls if "CALLS" in q]
        assert len(calls_calls) == 1
        assert calls_calls[0][1]["target_name"] == "other-proj"

    def test_empty_scan(self):
        neo4j = RecordingNeo4j()
        writer = StructureGraphWriter(neo4j=neo4j)
        scan = _make_scan(
            directories=[], files=[], dependencies=[],
            imports=[], test_edges=[], endpoints=[],
            cross_project_refs=[],
        )

        result = writer.write_project(scan, session_id="s1", user_id="u1")

        # Should still create the project entity
        assert result["entities"] == 1
        assert result["edges"] == 0

    def test_get_scan_fingerprint_none(self):
        neo4j = RecordingNeo4j()
        writer = StructureGraphWriter(neo4j=neo4j)
        assert writer.get_scan_fingerprint("nonexistent") is None

    def test_get_scan_fingerprint_found(self):
        neo4j = MagicMock()
        neo4j.execute.return_value = [{"fp": "abc123"}]
        writer = StructureGraphWriter(neo4j=neo4j)
        assert writer.get_scan_fingerprint("myproj") == "abc123"

    def test_idempotent_merge(self):
        """Running write_project twice should use MERGE (not CREATE)."""
        neo4j = RecordingNeo4j()
        writer = StructureGraphWriter(neo4j=neo4j)
        scan = _make_scan()

        writer.write_project(scan, session_id="s1", user_id="u1")

        # Verify all entity queries use MERGE
        for query, _ in neo4j.calls:
            if "Entity" in query and ("SET" in query or "CREATE" in query):
                assert "MERGE" in query, f"Expected MERGE in: {query[:80]}"


class TestWriteDocument:
    def test_write_document_issues_merge(self):
        """write_document should issue a MERGE with structure_role='document'."""
        neo4j = RecordingNeo4j()
        writer = StructureGraphWriter(neo4j=neo4j)

        writer.write_document(
            "/abs/path/to/DESIGN.md",
            "# Design doc content",
            project="cth.mcp.memory",
            structure_path="/abs/path/to/DESIGN.md",
            session_id="s1",
            user_id="u1",
        )

        merge_calls = [(q, p) for q, p in neo4j.calls if "MERGE" in q]
        assert len(merge_calls) == 1
        params = merge_calls[0][1]
        assert params["sp"] == "cth.mcp.memory"
        assert params["spath"] == "/abs/path/to/DESIGN.md"
        assert params["role"] == "document"

    def test_write_document_stores_root_path(self):
        """Extra dict must include root_path pointing to the original file."""
        neo4j = RecordingNeo4j()
        writer = StructureGraphWriter(neo4j=neo4j)

        writer.write_document(
            "/abs/path/to/DESIGN.md",
            "content",
            project="cth.mcp.memory",
            structure_path="/abs/path/to/DESIGN.md",
            session_id="s1",
            user_id="u1",
        )

        merge_calls = [(q, p) for q, p in neo4j.calls if "MERGE" in q]
        extra = merge_calls[0][1].get("extra", {})
        assert extra.get("root_path") == "/abs/path/to/DESIGN.md"
        assert extra.get("source") == "document-ingest"

    def test_write_document_name_is_filename(self):
        """Entity name should be the basename of the file path."""
        neo4j = RecordingNeo4j()
        writer = StructureGraphWriter(neo4j=neo4j)

        writer.write_document(
            "/some/dir/README.md",
            "content",
            project="myproj",
            structure_path="/some/dir/README.md",
            session_id="s1",
            user_id="u1",
        )

        merge_calls = [(q, p) for q, p in neo4j.calls if "MERGE" in q]
        assert merge_calls[0][1]["name"] == "README.md"

    def test_two_docs_same_name_different_dirs_separate_nodes(self):
        """Same filename in different directories must produce distinct structure_paths."""
        neo4j = RecordingNeo4j()
        writer = StructureGraphWriter(neo4j=neo4j)

        writer.write_document(
            "/proj/docs/README.md", "docs content",
            project="myproj", structure_path="/proj/docs/README.md",
            session_id="s1", user_id="u1",
        )
        writer.write_document(
            "/proj/api/README.md", "api content",
            project="myproj", structure_path="/proj/api/README.md",
            session_id="s1", user_id="u1",
        )

        merge_calls = [(q, p) for q, p in neo4j.calls if "MERGE" in q]
        paths = [p["spath"] for _, p in merge_calls]
        assert paths[0] != paths[1]


class TestQueryDocuments:
    def test_query_documents_no_filter(self):
        """query_documents should issue a MATCH for structure_role='document'."""
        neo4j = RecordingNeo4j()
        writer = StructureGraphWriter(neo4j=neo4j)

        result = writer.query_documents("cth.mcp.memory")

        assert isinstance(result, list)
        match_calls = [(q, p) for q, p in neo4j.calls if "document" in q and "$p" in q]
        assert len(match_calls) == 1
        assert match_calls[0][1]["p"] == "cth.mcp.memory"

    def test_query_documents_with_path_filter(self):
        """path_filter should be forwarded as $prefix in the Cypher query."""
        neo4j = RecordingNeo4j()
        writer = StructureGraphWriter(neo4j=neo4j)

        writer.query_documents("cth.mcp.memory", path_filter="/some/dir")

        match_calls = [(q, p) for q, p in neo4j.calls if "document" in q and "$prefix" in q]
        assert len(match_calls) == 1
        assert match_calls[0][1]["prefix"] == "/some/dir"


class TestIncrementalDiffAndHeat:
    """Tests for per-file mtime incremental diff and heat tracking."""

    def _calls_matching(self, neo4j: RecordingNeo4j, fragment: str) -> list[tuple[str, dict]]:
        return [(q, p) for q, p in neo4j.calls if fragment in q]

    def test_first_scan_no_heat_increment(self):
        """First scan: get_file_mtimes returns empty → changed_paths=None → _increment_heat not called."""
        neo4j = RecordingNeo4j()  # returns [] for all queries → empty stored mtimes
        writer = StructureGraphWriter(neo4j=neo4j)
        writer.write_project(_make_scan(), session_id="s1", user_id="u1")

        heat_calls = self._calls_matching(neo4j, "hot_count")
        # ON CREATE SET includes hot_count = 0, but no standalone increment query
        increment_calls = [c for c in heat_calls if "coalesce(n.hot_count, 0) + 1" in c[0]]
        assert increment_calls == [], "First scan must not increment hot_count"

    def test_incremental_scan_increments_heat_for_changed_files(self):
        """Re-scan with changed files: _increment_heat called with changed paths only."""
        neo4j = MagicMock()
        # src/main.py mtime differs (100 → 999); src/service.py unchanged (200 → 200)
        neo4j.execute.side_effect = lambda query, params=None: (
            [{"path": "src/main.py", "mtime": 100.0}, {"path": "src/service.py", "mtime": 200.0}]
            if "file_mtime IS NOT NULL" in query
            else [{"cnt": 1}] if "count(r)" in query
            else []
        )

        writer = StructureGraphWriter(neo4j=neo4j)
        scan = _make_scan(
            files=[
                FileEntry(rel_path="src/main.py", role="entrypoint", file_mtime=999.0),  # changed
                FileEntry(rel_path="src/service.py", role="file", file_mtime=200.0),      # unchanged
            ]
        )
        writer.write_project(scan, session_id="s1", user_id="u1")

        # Find the _increment_heat Cypher call (contains "+ 1")
        heat_calls = [
            c for c in neo4j.execute.call_args_list
            if "+ 1" in c.args[0]
        ]
        assert len(heat_calls) == 1
        paths = heat_calls[0].args[1]["paths"]
        assert "src/main.py" in paths
        assert "src/service.py" not in paths

    def test_incremental_scan_no_changes_skips_heat(self):
        """Re-scan with no changed files: changed_paths is empty set → _increment_heat not called."""
        neo4j = MagicMock()
        # Return identical mtimes for all files
        neo4j.execute.side_effect = lambda query, params=None: (
            [{"path": "src/main.py", "mtime": 100.0}, {"path": "src/service.py", "mtime": 200.0}]
            if "file_mtime IS NOT NULL" in query
            else [{"cnt": 1}] if "count(r)" in query
            else []
        )

        writer = StructureGraphWriter(neo4j=neo4j)
        scan = _make_scan(
            files=[
                FileEntry(rel_path="src/main.py", role="entrypoint", file_mtime=100.0),
                FileEntry(rel_path="src/service.py", role="file", file_mtime=200.0),
            ]
        )
        writer.write_project(scan, session_id="s1", user_id="u1")

        heat_increment_calls = [
            c for c in neo4j.execute.call_args_list
            if "hot_count" in str(c) and "+ 1" in str(c)
        ]
        assert heat_increment_calls == [], "No changes → hot_count must not be incremented"

    def test_query_files_includes_hot_count_when_nonzero(self):
        """query_files returns hot_count key only when value > 0."""
        neo4j = MagicMock()
        neo4j.execute.return_value = [
            {"path": "src/hot.py", "role": "file", "description": "hot file", "hot_count": 5},
            {"path": "src/cold.py", "role": "file", "description": "cold file", "hot_count": 0},
        ]
        writer = StructureGraphWriter(neo4j=neo4j)
        result = writer.query_files("proj")

        hot = next(r for r in result if r["path"] == "src/hot.py")
        cold = next(r for r in result if r["path"] == "src/cold.py")
        assert hot["hot_count"] == 5
        assert "hot_count" not in cold

    def test_query_files_cypher_includes_hot_count(self):
        """query_files Cypher selects hot_count from the graph."""
        neo4j = MagicMock()
        neo4j.execute.return_value = []
        writer = StructureGraphWriter(neo4j=neo4j)
        writer.query_files("proj")

        query = neo4j.execute.call_args[0][0]
        assert "hot_count" in query

    def test_merge_entities_batch_initializes_hot_count(self):
        """_merge_entities_batch ON CREATE SET includes hot_count = 0."""
        neo4j = RecordingNeo4j()
        writer = StructureGraphWriter(neo4j=neo4j)
        writer.write_project(_make_scan(), session_id="s1", user_id="u1")

        batch_queries = [q for q, _ in neo4j.calls if "UNWIND $rows" in q and "hot_count" in q]
        assert batch_queries, "File batch MERGE must initialize hot_count"
        assert "hot_count = 0" in batch_queries[0]


# ---------------------------------------------------------------------------
# Caller path normalization
# ---------------------------------------------------------------------------

def test_normalize_structure_path_matches_stored_spelling():
    """Stored paths are forward-slashed and repo-relative; callers pass input verbatim.

    Without normalization a Windows-style or dot-prefixed path misses the exact-match
    lookup and is reported as un-indexed -- a false refusal for a file that is present.
    """
    from menhir.infrastructure.structure_queries import _normalize_structure_path

    for variant in (
        "src/a.py",
        "./src/a.py",
        r"src\a.py",
        r".\src\a.py",
        "/src/a.py",
        "src/a.py ",
    ):
        assert _normalize_structure_path(variant) == "src/a.py", variant


# ---------------------------------------------------------------------------
# Stale entity pruning
#
# Files are pruned off the stored-mtime diff; directories and endpoints carry no mtime and
# had no pruning path at all, so they accumulated forever. The archolith umbrella kept 7,103
# directory entities and 102 endpoints after dropping from 1,977 indexed files to 8.
# ---------------------------------------------------------------------------

class TestStalePruning:
    def _writer(self):
        from menhir.infrastructure.structure_queries import StructureGraphWriter

        neo = RecordingNeo4j()
        return StructureGraphWriter(neo), neo

    def test_stale_directories_are_deleted(self):
        writer, neo = self._writer()

        writer._delete_stale_directories("p", ["src", "src/app"])

        q, params = neo.calls[-1]
        assert "structure_role: 'directory'" in q
        assert "NOT d.structure_path IN $keep" in q
        assert "DETACH DELETE" in q
        assert params["keep"] == ["src", "src/app"]
        assert params["project"] == "p"

    def test_empty_keep_list_deletes_nothing(self):
        """A scan finding no directories is indistinguishable from one that failed to
        populate them. Wiping the whole tree on an empty list is not worth the risk."""
        writer, neo = self._writer()

        assert writer._delete_stale_directories("p", []) == 0
        assert neo.calls == [], "must not issue a delete at all"

    def test_stale_endpoints_are_deleted_by_role(self):
        writer, neo = self._writer()

        writer._delete_stale_role_entities("p", "endpoint", ["endpoint:a"])

        q, params = neo.calls[-1]
        assert "structure_role: $role" in q
        assert params["role"] == "endpoint"
        assert params["keep"] == ["endpoint:a"]

    def test_empty_keep_list_deletes_every_entity_of_that_role(self):
        """Zero must be expressible: a complete scan that finds no endpoints means none.

        This inverts the previous contract. Guarding on an empty keep-list made "this project
        exposes nothing" unreachable, so the archolith umbrella kept 102 endpoints belonging to
        nested repos it had stopped indexing. Truncation is now handled by the caller, which
        skips the prune entirely while `partial_index` is true -- see the gate tests below.
        """
        writer, neo = self._writer()

        writer._delete_stale_role_entities("p", "endpoint", [])

        q, params = neo.calls[-1]
        assert "WHERE NOT n.structure_path IN $keep" in q
        assert params["keep"] == []
        assert params["role"] == "endpoint"

    def test_stale_files_pruned_across_all_four_roles_at_once(self):
        """A path may change role between scans; pruning role-by-role would delete it."""
        writer, neo = self._writer()

        writer._delete_stale_role_entities_multi(
            "p", ["file", "entrypoint", "config", "test"], ["a.py"]
        )

        q, params = neo.calls[-1]
        assert "n.structure_role IN $roles" in q
        assert params["roles"] == ["file", "entrypoint", "config", "test"]
        assert params["keep"] == ["a.py"]
        assert "sym" in q, "orphaned symbols must go with the file"

    def test_multi_role_prune_respects_empty_keep_list(self):
        writer, neo = self._writer()

        assert writer._delete_stale_role_entities_multi("p", ["file"], []) == 0
        assert neo.calls == []


class TestTruncatedScanNeverPrunesFiles:
    """The guard that keeps a capacity limit from becoming data loss.

    Unlike directories, files are subject to `_MAX_KEY_FILES`. When the cap binds,
    `scan.files` is a truncated view — a set-difference against it would delete every
    eligible file the cap happened to drop.
    """

    def _writer(self):
        from menhir.infrastructure.structure_queries import StructureGraphWriter

        neo = RecordingNeo4j()
        return StructureGraphWriter(neo), neo

    def test_partial_scan_issues_no_file_prune(self):
        from menhir.infrastructure.project_scanner import FileEntry

        writer, neo = self._writer()
        scan = _make_scan(
            files=[FileEntry(rel_path="a.py", role="file", description="")],
            files_discovered=100,
            files_eligible=100,
            files_indexed=1,          # cap bound hard
        )
        assert scan.partial_index is True

        writer.write_project(scan, session_id="s", user_id="u")

        prunes = [
            q for q, _ in neo.calls
            if "structure_role IN $roles" in q and "DETACH DELETE" in q
        ]
        assert prunes == [], "a truncated scan must never prune files"

    def test_complete_scan_does_prune_files(self):
        from menhir.infrastructure.project_scanner import FileEntry

        writer, neo = self._writer()
        scan = _make_scan(
            files=[FileEntry(rel_path="a.py", role="file", description="")],
            files_discovered=1,
            files_eligible=1,
            files_indexed=1,
        )
        assert scan.partial_index is False

        writer.write_project(scan, session_id="s", user_id="u")

        prunes = [
            q for q, _ in neo.calls
            if "structure_role IN $roles" in q and "DETACH DELETE" in q
        ]
        assert len(prunes) == 1

    def test_partial_scan_prunes_neither_endpoints_nor_dependencies(self):
        """Endpoints derive from scan.files, so the cap truncates them the same way."""
        from menhir.infrastructure.project_scanner import FileEntry

        writer, neo = self._writer()
        scan = _make_scan(
            files=[FileEntry(rel_path="a.py", role="file", description="")],
            files_discovered=100,
            files_eligible=100,
            files_indexed=1,
        )
        assert scan.partial_index is True

        writer.write_project(scan, session_id="s", user_id="u")

        role_prunes = [
            params.get("role")
            for q, params in neo.calls
            if "structure_role: $role" in q and "DETACH DELETE" in q
        ]
        assert role_prunes == [], "a truncated scan must not prune endpoints or dependencies"

    def test_complete_scan_prunes_endpoints_and_dependencies(self):
        from menhir.infrastructure.project_scanner import FileEntry

        writer, neo = self._writer()
        scan = _make_scan(
            files=[FileEntry(rel_path="a.py", role="file", description="")],
            files_discovered=1,
            files_eligible=1,
            files_indexed=1,
        )
        assert scan.partial_index is False

        writer.write_project(scan, session_id="s", user_id="u")

        role_prunes = {
            params.get("role")
            for q, params in neo.calls
            if "structure_role: $role" in q and "DETACH DELETE" in q
        }
        assert role_prunes == {"endpoint", "dependency"}

    def test_contains_repo_edges_are_pruned_to_the_current_scan(self):
        """The umbrella's own edge must go when a sub-repo leaves; the child project stays."""
        from menhir.infrastructure.project_scanner import FileEntry, NestedRepo

        writer, neo = self._writer()
        scan = _make_scan(
            files=[FileEntry(rel_path="a.py", role="file", description="")],
            files_discovered=1,
            files_eligible=1,
            files_indexed=1,
            nested_repos=[NestedRepo(rel_path="sub-a", name="sub-a")],
        )

        writer.write_project(scan, session_id="s", user_id="u")

        prunes = [
            (q, params) for q, params in neo.calls if "[r:CONTAINS_REPO]" in q and "DELETE r" in q
        ]
        assert len(prunes) == 1
        q, params = prunes[0]
        assert params["keep"] == ["sub-a"]
        assert "DETACH DELETE" not in q, "must delete the edge only, never the child project"
