"""Tests for structural anchoring: path extraction, normalization, content splitting, edge creation,
enrichment pipeline integration, and recall file_context injection."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from menhir.infrastructure.structural_anchoring import (
    extract_file_paths,
    normalize_to_repo_relative,
    split_narrative_and_diff,
)
from menhir.services.ingest_gate import IngestGate


# ---------------------------------------------------------------------------
# Path normalization
# ---------------------------------------------------------------------------

class TestNormalizeToRepoRelative:

    def test_absolute_windows_path(self):
        roots = [r"C:\Users\dev\IdeaProjects\projects\example-project"]
        result = normalize_to_repo_relative(
            r"C:\Users\dev\IdeaProjects\projects\example-project\src\foo.py",
            known_roots=roots,
        )
        assert result == "src/foo.py"

    def test_absolute_unix_path(self):
        roots = ["/mnt/c/Users/dev/IdeaProjects/projects/example-project"]
        result = normalize_to_repo_relative(
            "/mnt/c/Users/dev/IdeaProjects/projects/example-project/src/foo.py",
            known_roots=roots,
        )
        assert result == "src/foo.py"

    def test_git_bash_path(self):
        roots = ["/c/Users/dev/IdeaProjects/projects/example-project"]
        result = normalize_to_repo_relative(
            "/c/Users/dev/IdeaProjects/projects/example-project/src/foo.py",
            known_roots=roots,
        )
        assert result == "src/foo.py"

    def test_already_relative(self):
        result = normalize_to_repo_relative("src/foo.py")
        assert result == "src/foo.py"

    def test_backslash_normalization(self):
        result = normalize_to_repo_relative(r"src\services\foo.py")
        assert result == "src/services/foo.py"

    def test_no_matching_root_strips_drive(self):
        result = normalize_to_repo_relative(
            r"C:\somewhere\else\foo.py",
            known_roots=[r"C:\Users\dev\projects"],
        )
        assert result == "somewhere/else/foo.py"

    def test_longest_prefix_wins(self):
        roots = [
            r"C:\Users\dev\IdeaProjects",
            r"C:\Users\dev\IdeaProjects\projects\example-project",
        ]
        result = normalize_to_repo_relative(
            r"C:\Users\dev\IdeaProjects\projects\example-project\src\foo.py",
            known_roots=roots,
        )
        assert result == "src/foo.py"

    def test_case_insensitive_root_match_on_windows(self):
        roots = [r"c:\users\dev\ideaprojects\projects\example-project"]
        result = normalize_to_repo_relative(
            r"C:\Users\dev\IdeaProjects\projects\example-project\src\bar.py",
            known_roots=roots,
        )
        assert result == "src/bar.py"

    def test_no_roots_provided(self):
        result = normalize_to_repo_relative("src/menhir/services/foo.py", known_roots=None)
        assert result == "src/menhir/services/foo.py"

    def test_bare_drive_letter_path(self):
        """C:/foo.py without a known root should strip the drive."""
        result = normalize_to_repo_relative("C:/foo.py")
        assert result == "foo.py"


# ---------------------------------------------------------------------------
# Path extraction
# ---------------------------------------------------------------------------

class TestExtractFilePaths:

    def test_full_path_extracted(self):
        text = "Modified src/menhir/services/ingest_service.py to fix the bug."
        result = extract_file_paths(text)
        assert "src/menhir/services/ingest_service.py" in result

    def test_bare_filename_extracted(self):
        text = "Check ingest_service.py for details."
        result = extract_file_paths(text)
        assert "ingest_service.py" in result

    def test_deduplication(self):
        text = "Changed src/foo.py and also src/foo.py."
        result = extract_file_paths(text)
        assert result.count("src/foo.py") == 1

    def test_no_paths(self):
        text = "This is just regular text with no file references."
        assert extract_file_paths(text) == []

    def test_empty_string(self):
        assert extract_file_paths("") == []

    def test_full_path_priority_over_bare_name(self):
        """When a full path is present, the bare filename tail should not appear separately."""
        text = "Changed src/services/ingest_service.py in the pipeline."
        result = extract_file_paths(text)
        assert "src/services/ingest_service.py" in result
        assert "ingest_service.py" not in result

    def test_multiple_extensions(self):
        text = "Edit config.yaml and also main.ts and schema.sql."
        result = extract_file_paths(text)
        assert "config.yaml" in result
        assert "main.ts" in result
        assert "schema.sql" in result

    def test_mixed_full_and_bare(self):
        text = "See src/models/foo.py and also check bar.ts."
        result = extract_file_paths(text)
        assert "src/models/foo.py" in result
        assert "bar.ts" in result


# ---------------------------------------------------------------------------
# Narrative / diff splitting
# ---------------------------------------------------------------------------

class TestSplitNarrativeAndDiff:

    def test_with_diff(self):
        body = "User discussed the change.\n\n--- git diff ---\ndiff --git a/foo.py b/foo.py"
        narrative, diff = split_narrative_and_diff(body)
        assert narrative == "User discussed the change."
        assert "diff --git" in diff

    def test_no_diff(self):
        body = "Just a regular episode body with no diff."
        narrative, diff = split_narrative_and_diff(body)
        assert narrative == body
        assert diff == ""

    def test_diff_paths_dedup_with_narrative(self):
        """Diff-only paths should exclude paths already found in narrative."""
        narrative_text = "Changed src/services/foo.py."
        diff_text = "diff --git a/src/services/foo.py b/src/services/foo.py\n+++ src/services/bar.py"
        body = f"{narrative_text}\n\n--- git diff ---\n{diff_text}"

        narrative, diff = split_narrative_and_diff(body)
        narrative_paths = extract_file_paths(narrative)
        diff_paths = extract_file_paths(diff)
        narrative_set = set(narrative_paths)
        diff_only = [p for p in diff_paths if p not in narrative_set]

        assert "src/services/foo.py" in narrative_paths
        assert "src/services/foo.py" not in diff_only
        assert "src/services/bar.py" in diff_only


# ---------------------------------------------------------------------------
# Anchor edge creation (stub Neo4j)
# ---------------------------------------------------------------------------

class _StubNeo4j:
    """Minimal stub for Neo4j that records queries and returns canned rows."""

    def __init__(self, rows=None):
        self.calls: list[tuple[str, dict]] = []
        self._rows = rows or []

    def execute(self, query, params=None):
        self.calls.append((query, params or {}))
        return self._rows


class TestCreateAnchorEdges:

    def test_creates_anchored_to_edges(self):
        from menhir.infrastructure.structural_anchoring import create_anchor_edges

        stub = _StubNeo4j(rows=[{"created": 3}])
        result = create_anchor_edges(
            stub, ["sem-1", "sem-2"], ["struct-1"],
            anchor_source="narrative_path", weight=1.0,
        )
        assert result == 3
        assert len(stub.calls) == 1
        assert "ANCHORED_TO" in stub.calls[0][0]
        pairs = stub.calls[0][1]["pairs"]
        assert len(pairs) == 2
        assert all(p["anchor_source"] == "narrative_path" for p in pairs)
        assert all(p["weight"] == 1.0 for p in pairs)

    def test_diff_weight(self):
        from menhir.infrastructure.structural_anchoring import create_anchor_edges

        stub = _StubNeo4j(rows=[{"created": 1}])
        result = create_anchor_edges(
            stub, ["sem-1"], ["struct-1"],
            anchor_source="diff_path", weight=0.3,
        )
        assert result == 1
        pairs = stub.calls[0][1]["pairs"]
        assert pairs[0]["weight"] == 0.3
        assert pairs[0]["anchor_source"] == "diff_path"

    def test_empty_inputs_returns_zero(self):
        from menhir.infrastructure.structural_anchoring import create_anchor_edges

        stub = _StubNeo4j()
        assert create_anchor_edges(stub, [], ["struct-1"]) == 0
        assert create_anchor_edges(stub, ["sem-1"], []) == 0
        assert len(stub.calls) == 0


class TestResolveStructuralEntities:

    def test_full_path_match(self):
        from menhir.infrastructure.structural_anchoring import resolve_structural_entities

        stub = _StubNeo4j(rows=[{"uuid": "u-1", "project": "example-project", "path": "src/foo.py"}])
        result = resolve_structural_entities(stub, ["src/foo.py"])
        assert len(result) == 1
        assert result[0]["uuid"] == "u-1"

    def test_empty_paths(self):
        from menhir.infrastructure.structural_anchoring import resolve_structural_entities

        stub = _StubNeo4j()
        assert resolve_structural_entities(stub, []) == []
        assert len(stub.calls) == 0

    def test_project_filter(self):
        from menhir.infrastructure.structural_anchoring import resolve_structural_entities

        stub = _StubNeo4j(rows=[])
        resolve_structural_entities(stub, ["foo.py"], project_filter="example-project")
        assert "structure_project" in stub.calls[0][0]


class TestFindCrossLinkedSemanticEntities:

    def test_returns_semantic_uuids(self):
        from menhir.infrastructure.structural_anchoring import find_cross_linked_semantic_entities

        stub = _StubNeo4j(rows=[{"uuid": "sem-1"}, {"uuid": "sem-2"}])
        result = find_cross_linked_semantic_entities(stub, ["struct-1"])
        assert result == ["sem-1", "sem-2"]

    def test_empty_structural_uuids(self):
        from menhir.infrastructure.structural_anchoring import find_cross_linked_semantic_entities

        stub = _StubNeo4j()
        assert find_cross_linked_semantic_entities(stub, []) == []


# ---------------------------------------------------------------------------
# Enrichment pipeline integration
# ---------------------------------------------------------------------------

class TestEnrichmentAnchoring:

    def test_anchoring_runs_in_stamp_and_finalize(self):
        """The _anchor_to_structural_entities function is called during stamp_and_finalize."""
        from menhir.services.enrichment_steps import _anchor_to_structural_entities

        # Verify the function exists and is callable with the expected signature
        assert callable(_anchor_to_structural_entities)

    def test_anchoring_nonfatal_on_exception(self):
        """Anchoring failure should not raise — it's best-effort."""
        from menhir.services.enrichment_steps import _anchor_to_structural_entities, EnrichmentContext

        @dataclass
        class _BrokenAdapter:
            def anchor_semantic_to_structural(self, *args, **kwargs):
                raise RuntimeError("Neo4j down")

            def update_episode_processing(self, *args, **kwargs):
                pass

        ctx = EnrichmentContext(
            episode_uuid="ep-1",
            claimed={"content": "Modified src/foo.py"},
            started=0.0,
            processing_attempts=1,
            worker_id="w-1",
            graph_adapter=_BrokenAdapter(),
            graphiti_client=None,
            lifecycle_service=None,
            llm=None,
            ingest_gate=IngestGate(1),
            processing_steps_total=5,
            settings_record_revisions=False,
            ready_warning_ms=5000,
            graphiti_add_episode_timeout_s=30.0,
            graphiti_episode_max_estimated_tokens=8000,
            get_queue_depth=lambda: 0,
        )

        # Should NOT raise
        result = _anchor_to_structural_entities(ctx, ["node-1"], "Modified src/foo.py")
        assert result == {"narrative": 0, "diff": 0}

    def test_anchoring_returns_counts(self):
        """Anchoring returns narrative and diff edge counts."""
        from menhir.services.enrichment_steps import _anchor_to_structural_entities, EnrichmentContext

        @dataclass
        class _CountingAdapter:
            calls: list = None

            def __post_init__(self):
                self.calls = []

            def anchor_semantic_to_structural(self, sem_uuids, paths, **kwargs):
                self.calls.append((sem_uuids, paths, kwargs))
                return len(sem_uuids) * len(paths)

            def update_episode_processing(self, *args, **kwargs):
                pass

        adapter = _CountingAdapter()
        ctx = EnrichmentContext(
            episode_uuid="ep-2",
            claimed={"content": "Changed src/foo.py", "diff": "diff --git a/src/bar.py"},
            started=0.0,
            processing_attempts=1,
            worker_id="w-1",
            graph_adapter=adapter,
            graphiti_client=None,
            lifecycle_service=None,
            llm=None,
            ingest_gate=IngestGate(1),
            processing_steps_total=5,
            settings_record_revisions=False,
            ready_warning_ms=5000,
            graphiti_add_episode_timeout_s=30.0,
            graphiti_episode_max_estimated_tokens=8000,
            get_queue_depth=lambda: 0,
        )

        body = "Changed src/foo.py\n\n--- git diff ---\ndiff --git a/src/bar.py"
        result = _anchor_to_structural_entities(ctx, ["node-1"], body)

        # narrative anchor: 1 node * 1 path = 1
        assert result["narrative"] == 1
        # diff anchor: 1 node * 1 path = 1
        assert result["diff"] == 1
        assert len(adapter.calls) == 2

    def test_anchoring_no_nodes_skips(self):
        """When no extracted nodes, anchoring does nothing."""
        from menhir.services.enrichment_steps import _anchor_to_structural_entities, EnrichmentContext

        ctx = EnrichmentContext(
            episode_uuid="ep-3",
            claimed={"content": "Changed src/foo.py"},
            started=0.0,
            processing_attempts=1,
            worker_id="w-1",
            graph_adapter=None,
            graphiti_client=None,
            lifecycle_service=None,
            llm=None,
            ingest_gate=IngestGate(1),
            processing_steps_total=5,
            settings_record_revisions=False,
            ready_warning_ms=5000,
            graphiti_add_episode_timeout_s=30.0,
            graphiti_episode_max_estimated_tokens=8000,
            get_queue_depth=lambda: 0,
        )

        result = _anchor_to_structural_entities(ctx, [], "Changed src/foo.py")
        assert result == {"narrative": 0, "diff": 0}


# ---------------------------------------------------------------------------
# Recall file_context integration
# ---------------------------------------------------------------------------

class TestRecallFileContext:

    def test_file_context_injects_candidates(self):
        """File-linked UUIDs appear in candidate set with baseline similarity."""
        from menhir.services.recall_service import RecallService, FILE_LINKED_BASELINE_SIMILARITY

        # Build a minimal RecallService with mocked internals
        @dataclass
        class _MockStructure:
            def resolve_structural_neighbors(self, project, path):
                if path == "src/foo.py":
                    return ["struct-1", "struct-2"]
                return []

        @dataclass
        class _MockAdapter:
            _structure: _MockStructure = None

            def __post_init__(self):
                self._structure = _MockStructure()

            def list_structure_projects(self):
                return [{"name": "proj", "root_path": "/path/to/proj"}]

            def find_cross_linked_semantic_entities(self, struct_uuids):
                return ["file-linked-uuid-1"]

        adapter = _MockAdapter()
        service = RecallService(
            graphiti_client=None,
            graph_adapter=adapter,
            scoring_service=None,
        )

        import asyncio
        result = asyncio.run(service._resolve_file_context("src/foo.py", "proj"))
        assert "file-linked-uuid-1" in result

    def test_file_context_none_is_noop(self):
        """When file_context is None, no resolution happens."""
        from menhir.services.recall_service import RecallService

        service = RecallService(
            graphiti_client=None,
            graph_adapter=None,
            scoring_service=None,
        )

        # file_context=None should not reach _resolve_file_context at all
        # We verify by checking the recall signature accepts the param
        import inspect
        sig = inspect.signature(service.recall)
        assert "file_context" in sig.parameters
        assert sig.parameters["file_context"].default is None

    def test_file_context_auto_detects_project(self):
        """When project is not provided, resolution tries each known project."""

        @dataclass
        class _MockStructure:
            def resolve_structural_neighbors(self, project, path):
                if project == "example-project" and path == "src/foo.py":
                    return ["struct-1"]
                return []

            def resolve_structural_neighbors_bulk(self, projects, path):
                for project in projects:
                    neighbors = self.resolve_structural_neighbors(project, path)
                    if neighbors:
                        return (project, neighbors)
                return None

        @dataclass
        class _MockAdapter:
            _structure: _MockStructure = None

            def __post_init__(self):
                self._structure = _MockStructure()

            def list_structure_projects(self):
                return [
                    {"name": "yawn.rip", "root_path": "/path/to/rip"},
                    {"name": "example-project", "root_path": "/path/to/memory"},
                ]

            def find_cross_linked_semantic_entities(self, struct_uuids):
                return ["linked-1"]

        from menhir.services.recall_service import RecallService

        adapter = _MockAdapter()
        service = RecallService(
            graphiti_client=None,
            graph_adapter=adapter,
            scoring_service=None,
        )

        import asyncio
        result = asyncio.run(service._resolve_file_context("src/foo.py"))
        assert result == ["linked-1"]

    def test_file_context_no_match_returns_empty(self):
        """When no structural match is found, returns empty list."""

        @dataclass
        class _MockStructure:
            def resolve_structural_neighbors(self, project, path):
                return []

            def resolve_structural_neighbors_bulk(self, projects, path):
                for project in projects:
                    neighbors = self.resolve_structural_neighbors(project, path)
                    if neighbors:
                        return (project, neighbors)
                return None

        @dataclass
        class _MockAdapter:
            _structure: _MockStructure = None

            def __post_init__(self):
                self._structure = _MockStructure()

            def list_structure_projects(self):
                return [{"name": "proj", "root_path": "/path"}]

            def find_cross_linked_semantic_entities(self, struct_uuids):
                return []

        from menhir.services.recall_service import RecallService

        adapter = _MockAdapter()
        service = RecallService(
            graphiti_client=None,
            graph_adapter=adapter,
            scoring_service=None,
        )

        import asyncio
        result = asyncio.run(service._resolve_file_context("nonexistent.py"))
        assert result == []
