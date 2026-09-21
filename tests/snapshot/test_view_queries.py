from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
from menhir.snapshot.view_queries import SNAPSHOT_STATUS_KEY, SnapshotStructureViewReader


def _view_row(**overrides):
    row = {
        "project_id": "project-1",
        "view_key": "canonical",
        "current_root": "root-current",
        "generation": 3,
        "degraded": False,
        "snapshot_id": "snapshot-1",
    }
    row.update(overrides)
    return row


class FakeNeo4j:
    def __init__(self, *, exact=None, named=None) -> None:
        self.exact = exact
        self.named = [] if named is None else named
        self.calls: list[tuple[str, dict]] = []

    def execute(self, statement: str, params: dict):
        self.calls.append((statement, params))
        if "true AS exact_match" in statement:
            return [] if self.exact is None else [self.exact]
        if "false AS exact_match" in statement:
            return self.named
        return []


def test_exact_project_id_wins_without_display_name_lookup() -> None:
    neo4j = FakeNeo4j(exact=_view_row())

    resolved = SnapshotStructureViewReader(neo4j).resolve("project-1")

    assert resolved is not None
    assert resolved.project_id == "project-1"
    assert not any("false AS exact_match" in statement for statement, _ in neo4j.calls)


def test_exact_view_without_current_root_falls_back_without_alias_lookup() -> None:
    neo4j = FakeNeo4j(exact=_view_row(current_root=None))

    result = SnapshotStructureViewReader(neo4j).query("project-1", "files")

    assert result is None
    assert not any("false AS exact_match" in statement for statement, _ in neo4j.calls)


def test_unambiguous_display_name_resolves_when_project_id_does_not() -> None:
    neo4j = FakeNeo4j(named=[_view_row(project_id="durable-id")])

    result = SnapshotStructureViewReader(neo4j).query("display-name", "dependencies")

    assert result is not None
    assert result[SNAPSHOT_STATUS_KEY]["project_id"] == "durable-id"


def test_display_name_lookup_can_be_disabled_at_an_identity_boundary() -> None:
    neo4j = FakeNeo4j(named=[_view_row(project_id="unrelated-remote")])

    result = SnapshotStructureViewReader(neo4j).query(
        "shared-name", "files", allow_display_name=False
    )

    assert result is None
    assert not any("false AS exact_match" in statement for statement, _ in neo4j.calls)


def test_ambiguous_display_name_fails_closed_with_stable_value_error() -> None:
    neo4j = FakeNeo4j(
        named=[_view_row(project_id="one"), _view_row(project_id="two")]
    )

    with pytest.raises(
        ValueError, match="^Ambiguous canonical snapshot display name: shared$"
    ):
        SnapshotStructureViewReader(neo4j).resolve("shared")


def test_degraded_status_is_json_safe_and_contains_no_root_or_path() -> None:
    neo4j = FakeNeo4j(exact=_view_row(degraded=True))

    result = SnapshotStructureViewReader(neo4j).query("project-1", "cross_refs")

    assert result is not None
    status = result[SNAPSHOT_STATUS_KEY]
    assert status == {
        "project_id": "project-1",
        "view_key": "canonical",
        "snapshot_id": "snapshot-1",
        "generation": 3,
        "degraded": True,
        "warning": "Canonical snapshot view is degraded; results may be unreliable.",
    }
    assert "root" not in repr(status).lower()
    assert "path" not in repr(status).lower()
    assert result["data"] == []


@pytest.mark.parametrize(
    ("query_type", "kwargs", "expected_type"),
    [
        ("overview", {}, dict),
        ("files", {}, list),
        ("imports", {"file_path": "src/a.py"}, dict),
        ("tests", {}, list),
        ("endpoints", {}, list),
        ("dependencies", {}, list),
        ("cross_refs", {}, list),
        ("blast_radius", {"file_paths": ["src/a.py"]}, dict),
        ("affected_tests", {"file_paths": ["src/a.py"]}, dict),
        ("symbols", {"path": "src/a.py"}, dict),
        ("context", {"path": "src/a.py"}, dict),
    ],
)
def test_every_structure_query_type_returns_legacy_data_in_an_envelope(
    query_type: str, kwargs: dict, expected_type: type
) -> None:
    neo4j = FakeNeo4j(exact=_view_row())

    result = SnapshotStructureViewReader(neo4j).query(
        "project-1", query_type, **kwargs
    )

    assert result is not None
    assert isinstance(result["data"], expected_type)
    assert result[SNAPSHOT_STATUS_KEY]["snapshot_id"] == "snapshot-1"


def test_every_snapshot_entity_match_is_root_scoped() -> None:
    neo4j = FakeNeo4j(exact=_view_row())
    reader = SnapshotStructureViewReader(neo4j)
    reader.query("project-1", "overview")
    reader.query("project-1", "imports", file_path="src/a.py")
    reader.query("project-1", "tests")
    reader.query("project-1", "blast_radius", file_paths=["src/a.py"])
    reader.query("project-1", "symbols", path="src/")
    reader.query("project-1", "context", path="src/a.py")

    snapshot_statements = [
        statement for statement, _ in neo4j.calls if "SnapshotEntity" in statement
    ]
    assert snapshot_statements
    assert all("view_root: $root" in statement for statement in snapshot_statements)
    assert all("project_id: $pid" in statement for statement in snapshot_statements)
    assert all(params.get("root") == "root-current" for statement, params in neo4j.calls
               if "SnapshotEntity" in statement)


def test_relationship_queries_scope_both_ends_to_the_current_root() -> None:
    neo4j = FakeNeo4j(exact=_view_row())

    SnapshotStructureViewReader(neo4j).query(
        "project-1", "imports", file_path="src/a.py"
    )

    relationship_statements = [
        statement for statement, _ in neo4j.calls if ":IMPORTS" in statement
    ]
    assert relationship_statements
    assert all(statement.count("view_root: $root") >= 2 for statement in relationship_statements)


def test_adapter_falls_back_with_unchanged_arguments_when_no_view_exists() -> None:
    adapter = object.__new__(MemoryGraphAdapter)
    adapter._snapshot_structure = MagicMock()
    adapter._snapshot_structure.query.return_value = None
    local = MagicMock()
    local.query_files.return_value = [{"path": "local.py"}]
    adapter._structure = local

    result = adapter.query_structure("project-1", "files", path_filter="src/")

    assert result == [{"path": "local.py"}]
    local.query_files.assert_called_once_with("project-1", path_filter="src/")


def test_adapter_prefers_snapshot_and_never_calls_local_reader() -> None:
    adapter = object.__new__(MemoryGraphAdapter)
    adapter._snapshot_structure = MagicMock()
    snapshot = {SNAPSHOT_STATUS_KEY: {}, "data": []}
    adapter._snapshot_structure.query.return_value = snapshot
    adapter._structure = MagicMock()

    assert adapter.query_structure("project-1", "files") is snapshot
    adapter._structure.query_files.assert_not_called()


def test_same_named_local_project_is_not_replaced_by_snapshot_alias() -> None:
    adapter = object.__new__(MemoryGraphAdapter)
    adapter._snapshot_structure = MagicMock()
    remote = {SNAPSHOT_STATUS_KEY: {"project_id": "remote-id"}, "data": []}
    adapter._snapshot_structure.query.side_effect = [None, remote]
    local = MagicMock()
    local.list_projects.return_value = [{"name": "shared"}]
    local.query_files.return_value = [{"path": "local.py"}]
    adapter._structure = local

    assert adapter.query_structure("shared", "files") == [{"path": "local.py"}]
    assert adapter._snapshot_structure.query.call_count == 1


def test_same_named_local_and_snapshot_projects_both_remain_listed() -> None:
    adapter = object.__new__(MemoryGraphAdapter)
    adapter._structure = MagicMock()
    adapter._structure.list_projects.return_value = [{"name": "shared"}]
    adapter._snapshot_structure = MagicMock()
    adapter._snapshot_structure.list_projects.return_value = [
        {"name": "shared", "project_id": "remote-id", "snapshot": True}
    ]

    assert adapter.list_structure_projects() == [
        {"name": "shared"},
        {"name": "shared", "project_id": "remote-id", "snapshot": True},
    ]


def test_unknown_query_type_is_rejected_before_either_reader() -> None:
    adapter = object.__new__(MemoryGraphAdapter)
    adapter._snapshot_structure = MagicMock()
    adapter._structure = SimpleNamespace()

    with pytest.raises(ValueError, match="^Unknown structure query type: private$"):
        adapter.query_structure("project-1", "private")

    adapter._snapshot_structure.query.assert_not_called()
