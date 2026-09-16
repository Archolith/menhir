"""#99 -- structure prunes must address the rows the identity gate authorised.

Offline half. Every assertion here is about the Cypher and parameters the writer EMITS: the
recorder never executes a statement, so it cannot decide whether the predicate deletes the right
nodes. That question belongs to `test_cf99_prune_identity_keying_online.py` and is not answerable
here -- these tests exist to catch a silent revert to name-only addressing, which is exactly what
a text assertion can see and a behavioural one would miss once the fake returns nothing either way.
"""

from __future__ import annotations

from typing import Any

import pytest

from menhir.infrastructure.project_scanner import (
    DirEntry,
    FileEntry,
    ProjectScanResult,
)
from menhir.infrastructure.structure_queries import StructureGraphWriter

pytestmark = pytest.mark.unit

_PID = "11111111-2222-3333-4444-555555555555"


class RecordingNeo4j:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def execute(
        self, query: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        self.calls.append((query, params or {}))
        return []


def _writer() -> tuple[StructureGraphWriter, RecordingNeo4j]:
    neo = RecordingNeo4j()
    return StructureGraphWriter(neo4j=neo), neo


def _prune_calls(neo: RecordingNeo4j) -> list[tuple[str, dict[str, Any]]]:
    return [(q, p) for q, p in neo.calls if "DELETE" in q.upper()]


# --- the predicate the helpers emit -------------------------------------------------------------


def test_identity_arm_and_legacy_arm_are_both_emitted() -> None:
    """Two indexed lookups, not one OR: an OR over both keys plans as a full label scan."""
    writer, neo = _writer()

    writer._delete_stale_role_entities("proj", "endpoint", [], _PID)

    queries = [q for q, _ in neo.calls]
    assert len(queries) == 2
    assert "n.structure_project_id = $project_id" in queries[0]
    assert "n.structure_project_id IS NULL" in queries[1]
    assert "n.structure_project = $project" in queries[1]


def test_legacy_arm_never_matches_a_row_stamped_for_another_project() -> None:
    """The property #99 is about: the legacy arm is guarded on the stamp being ABSENT.

    Without `IS NULL` the second arm is the old name-keyed delete, and a row carrying another
    project's id but the same display name is inside the delete set again.
    """
    writer, neo = _writer()

    writer._delete_stale_role_entities("proj", "endpoint", [], _PID)

    legacy = neo.calls[1][0]
    assert "structure_project_id IS NULL AND" in " ".join(legacy.split())


def test_no_prune_matches_on_name_alone_when_an_identity_is_known() -> None:
    """Sweep every prune helper: none may address rows by display name without the NULL guard."""
    writer, neo = _writer()

    writer.get_file_mtimes("proj", _PID)
    writer._delete_file_entities("proj", ["a.py"], _PID)
    writer._delete_stale_role_entities("proj", "endpoint", ["e"], _PID)
    writer._delete_stale_role_entities_multi("proj", ["file"], ["a.py"], _PID)
    writer._delete_stale_contains_repo_edges("proj", ["sub"], _PID)
    writer._delete_stale_directories("proj", ["src"], _PID)

    assert neo.calls, "expected the helpers to emit statements"
    for query, params in neo.calls:
        compact = " ".join(query.split())
        assert params["project_id"] == _PID
        if "structure_project = $project" in compact:
            assert "structure_project_id IS NULL AND" in compact, compact


def test_every_helper_carries_the_identity_parameter() -> None:
    writer, neo = _writer()

    writer._delete_stale_directories("proj", ["src"], _PID)

    for _query, params in neo.calls:
        assert params["project_id"] == _PID
        assert params["project"] == "proj"


# --- degradation when no identity was settled ---------------------------------------------------


def test_without_an_identity_the_helpers_keep_the_old_name_only_behaviour() -> None:
    """An id-less scan passed no identity gate, so there is no validated identity to diverge from.

    Refusing here would change behaviour on a path CF-257 is separately closing; this is the
    residual hole #99 leaves open, and it shuts when `project_id` becomes mandatory.
    """
    writer, neo = _writer()

    writer._delete_stale_role_entities("proj", "endpoint", [], None)

    assert len(neo.calls) == 1
    query = " ".join(neo.calls[0][0].split())
    assert "n.structure_project = $project" in query
    assert "structure_project_id" not in query


# --- the writer threads it through --------------------------------------------------------------


def _scan(**overrides: Any) -> ProjectScanResult:
    defaults: dict[str, Any] = {
        "name": "proj",
        "root_path": "/tmp/proj",
        "stack": "python",
        "description": "",
        "directories": [DirEntry(rel_path="src")],
        "files": [FileEntry(rel_path="src/main.py", role="file", file_mtime=5.0)],
        "project_id": _PID,
        "files_discovered": 1,
        "files_eligible": 1,
        "files_indexed": 1,
    }
    defaults.update(overrides)
    return ProjectScanResult(**defaults)


def test_write_project_passes_the_scans_identity_to_every_prune() -> None:
    """The gate validates `project_id`; the deletes must run under that same id, not the name."""
    writer, neo = _writer()

    writer.write_project(_scan(), session_id="s1", user_id="u1")

    pruned = _prune_calls(neo)
    assert pruned, "a complete scan must reach its prunes"
    for query, params in pruned:
        assert params.get("project_id") == _PID, query


def test_read_back_of_mtimes_is_identity_keyed() -> None:
    """The rename path: a name-keyed read returned {} and sent the writer down first-scan."""
    writer, neo = _writer()

    writer.write_project(_scan(), session_id="s1", user_id="u1")

    mtime_reads = [
        (q, p) for q, p in neo.calls if "file_mtime IS NOT NULL" in q
    ]
    assert mtime_reads
    for query, params in mtime_reads:
        assert params["project_id"] == _PID, query
