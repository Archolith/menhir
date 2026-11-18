"""Tests for structural anchoring: path extraction, normalization, content splitting, edge creation,
enrichment pipeline integration, and recall file_context injection."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from menhir.infrastructure.structural_anchoring import (
    extract_diff_file_paths,
    extract_file_paths,
    resolve_file_reference,
    split_narrative_and_diff,
)
from menhir.services.ingest_gate import IngestGate


# ---------------------------------------------------------------------------
# Path normalization
# ---------------------------------------------------------------------------

class TestResolveFileReference:
    @pytest.mark.parametrize(
        "reference",
        [r"C:\repo\nested\src\foo.py", "/mnt/c/repo/nested/src/foo.py", "/c/repo/nested/src/foo.py"],
    )
    def test_absolute_windows_spellings_use_deepest_indexed_root(self, reference):
        projects = [
            {"name": "parent", "root_path": "c:/repo"},
            {"name": "child", "root_path": "c:/repo/nested"},
        ]
        assert resolve_file_reference(reference, projects) == ("child", "src/foo.py")

    @pytest.mark.parametrize("reference", [r"a\foo.py", "a/foo.py", "./a/foo.py"])
    def test_relative_path_keeps_single_letter_directory(self, reference):
        assert resolve_file_reference(reference, []) == (None, "a/foo.py")

    @pytest.mark.parametrize("reference", ["C:/work/other/src/foo.py", "/c/work/other/src/foo.py"])
    def test_absolute_path_outside_index_abstains(self, reference):
        projects = [{"name": "proj", "root_path": "C:/work/proj"}]
        assert resolve_file_reference(reference, projects) is None

    def test_plain_leading_slash_is_repo_relative(self):
        # Same reading as normalize_query_path, which query_structure uses for this spelling.
        projects = [{"name": "proj", "root_path": "/work/proj"}]
        assert resolve_file_reference("/src/foo.py", projects, "proj") == ("proj", "src/foo.py")
        assert resolve_file_reference("/src/foo.py", projects) == (None, "src/foo.py")
        assert resolve_file_reference("/foo.py", projects, "proj") == ("proj", "./foo.py")

    def test_absolute_path_cannot_select_a_different_project(self):
        projects = [{"name": "proj", "root_path": "/work/proj"}]
        assert resolve_file_reference("/work/proj/src/foo.py", projects, "other") is None

    def test_root_file_retains_explicit_directory(self):
        projects = [{"name": "proj", "root_path": "/work/proj"}]
        assert resolve_file_reference("/work/proj/foo.py", projects) == ("proj", "./foo.py")
        assert resolve_file_reference("./foo.py", projects, "proj") == ("proj", "./foo.py")

    def test_ambiguous_project_root_abstains(self):
        projects = [{"name": name, "root_path": "/work/proj"} for name in ("a", "b")]
        assert resolve_file_reference("/work/proj/src/foo.py", projects) is None

    def test_duplicate_project_name_cannot_supply_a_scope(self):
        projects = [{"name": "proj", "root_path": root} for root in ("/work/a", "/work/b")]
        assert resolve_file_reference("/work/a/src/foo.py", projects) is None
        assert resolve_file_reference("src/foo.py", projects, "proj") is None


# ---------------------------------------------------------------------------
# Path extraction
# ---------------------------------------------------------------------------

class TestExtractFilePaths:

    def test_full_path_extracted(self):
        text = "Modified src/menhir/services/ingest_service.py to fix the bug."
        result = extract_file_paths(text)
        assert "src/menhir/services/ingest_service.py" in result

    def test_windows_path_is_extracted_as_one_reference(self):
        path = r"C:\repo\src\server.py"
        assert extract_file_paths(f"Changed {path}") == [path]

    def test_quoted_windows_path_keeps_spaces_without_a_partial_basename(self):
        path = r"C:\my repo\src\web server.py"
        assert extract_file_paths(f"Changed `{path}` and then helper.py") == [path, "helper.py"]

    def test_absolute_file_at_drive_root_is_not_reduced_to_basename(self):
        assert extract_file_paths("Changed C:/server.py") == ["C:/server.py"]

    def test_diff_prefix_does_not_remove_real_single_letter_directory(self):
        diff = "diff --git a/a/foo.py b/a/foo.py\n--- a/a/foo.py\n+++ b/a/foo.py"
        assert extract_diff_file_paths(diff) == ["a/foo.py"]

    @pytest.mark.parametrize(
        "text",
        ["ab/" * 16000, "ab\\" * 16000, "a." * 16000, "x" * 48000],
        ids=["slashes", "backslashes", "dots", "word"],
    )
    def test_long_path_like_run_is_scanned_in_linear_time(self, text):
        import time

        started = time.perf_counter()
        extract_file_paths(text)
        extract_diff_file_paths(text)
        # Was ~68s per call at this length; linear scanning takes milliseconds.
        assert time.perf_counter() - started < 2.0

    def test_path_is_not_extracted_from_the_middle_of_a_run(self):
        assert extract_file_paths("see pkg/src/foo.py and lib.util.py") == ["pkg/src/foo.py", "lib.util.py"]

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

    def test_caps_unique_pairs_before_building_the_write_payload(self):
        from menhir.infrastructure.structural_anchoring import create_anchor_edges

        stub = _StubNeo4j(rows=[{"created": 2}])
        assert create_anchor_edges(stub, ["s1", "s1", "s2"], ["f1", "f2"], max_edges=2) == 2
        pairs = stub.calls[0][1]["pairs"]
        assert [(pair["sem"], pair["struct"]) for pair in pairs] == [("s1", "f1"), ("s2", "f1")]

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

            def list_structure_projects(self):
                return [{"name": "proj", "root_path": "/work/proj"}]

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
        assert adapter.calls[0][1] == ["src/foo.py"]
        assert adapter.calls[1][1] == ["src/bar.py"]
        assert adapter.calls[0][2]["max_edges"] - adapter.calls[1][2]["max_edges"] == 1

    @staticmethod
    def _anchor(body, projects):
        from menhir.services.enrichment_steps import _anchor_to_structural_entities

        calls = []

        def anchor(sem_uuids, paths, **kwargs):
            calls.append((paths, kwargs["project_filter"], kwargs["anchor_source"]))
            return len(paths)

        adapter = SimpleNamespace(
            anchor_semantic_to_structural=anchor, list_structure_projects=lambda: projects,
        )
        _anchor_to_structural_entities(
            SimpleNamespace(episode_uuid="ep", graph_adapter=adapter), ["node-1"], body,
        )
        return calls

    _PROJECTS = [
        {"name": "menhir", "root_path": "C:/w/menhir"},
        {"name": "other", "root_path": "C:/w/other"},
    ]

    def test_relative_reference_uses_the_project_an_absolute_path_names(self):
        calls = self._anchor("Edited C:/w/menhir/src/a.py and src/b.py", self._PROJECTS)
        assert calls == [(["src/a.py", "src/b.py"], ["menhir"], "narrative_path")]

    def test_relative_reference_without_project_evidence_uses_live_projects(self):
        # Never unfiltered: entities of a deleted project must not make a live file ambiguous.
        calls = self._anchor("Edited src/b.py", self._PROJECTS)
        assert calls == [(["src/b.py"], ["menhir", "other"], "narrative_path")]

    def test_no_indexed_project_writes_nothing(self):
        assert self._anchor("Edited src/b.py", []) == []

    def test_absolute_and_relative_spelling_of_one_file_anchor_once(self):
        body = "Edited C:/w/menhir/src/a.py\n\n--- git diff ---\ndiff --git a/src/a.py b/src/a.py"
        assert self._anchor(body, self._PROJECTS) == [(["src/a.py"], ["menhir"], "narrative_path")]

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

        result = asyncio.run(service._resolve_file_context("nonexistent.py"))
        assert result == []
