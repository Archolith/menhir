"""TODO file-link cardinality against a disposable Neo4j test instance."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from menhir.infrastructure.structure_queries import StructureGraphWriter
from menhir.infrastructure.todo_repository import TodoRepository

pytestmark = [pytest.mark.online, pytest.mark.timeout(60)]


def _file(
    graph: Any, project: str, path: str, *, namespace: str | None = None
) -> str:
    file_uuid = str(uuid4())
    graph.execute(
        """CREATE (f:Entity {uuid: $uuid, structure_project: $project,
        structure_path: $path, structure_role: 'file', namespace: $namespace})""",
        {"uuid": file_uuid, "project": project, "path": path, "namespace": namespace},
    )
    return file_uuid


def _edges(graph: Any, todo_uuid: str) -> list[dict[str, Any]]:
    return graph.execute(
        """MATCH (t:Todo {uuid: $uuid})-[:REFERENCES_FILE]->(f:Entity)
        RETURN f.uuid AS uuid, f.structure_project AS project, f.structure_path AS path
        ORDER BY path, project""",
        {"uuid": todo_uuid},
    )


def _locations(graph: Any, todo_uuid: str) -> list[dict[str, Any]]:
    return graph.execute(
        """MATCH (t:Todo {uuid: $uuid})-[:HAS_LOCATION]->(l:TodoLocation)
        RETURN l.ordinal AS ordinal, l.path AS path, l.project AS project,
               l.resolution_status AS status, l.unresolved_reason AS reason
        ORDER BY ordinal""",
        {"uuid": todo_uuid},
    )


def test_ambiguous_path_stays_unlinked_but_explicit_project_links_one(
    test_neo4j_repo: Any,
) -> None:
    graph = test_neo4j_repo
    _file(graph, "alpha", "src/main.py")
    alpha_uuid = _file(graph, "beta", "src/main.py")
    repo = TodoRepository(graph)

    ambiguous = repo.create_todo(content="ambiguous", code_ref="src/main.py:7")
    assert ambiguous["linked_files"] == []
    assert ambiguous["locations"][0]["unresolved_reason"] == "ambiguous_file"
    assert _edges(graph, ambiguous["uuid"]) == []
    assert _locations(graph, ambiguous["uuid"]) == [{
        "ordinal": 0, "path": "src/main.py", "project": None,
        "status": "unresolved", "reason": "ambiguous_file",
    }]

    explicit = repo.create_todo(
        content="beta", code_ref="src/main.py:7", structure_project="beta"
    )
    assert explicit["linked_files"] == [
        {"ordinal": 0, "path": "src/main.py", "project": "beta"}
    ]
    assert _edges(graph, explicit["uuid"]) == [
        {"uuid": alpha_uuid, "project": "beta", "path": "src/main.py"}
    ]


def test_resolved_unqualified_location_keeps_its_project_after_another_project_appears(
    test_neo4j_repo: Any,
) -> None:
    graph = test_neo4j_repo
    _file(graph, "alpha", "src/main.py")
    repo = TodoRepository(graph)
    created = repo.create_todo(content="alpha work", code_ref="src/main.py:7")
    assert created["locations"][0]["project"] == "alpha"
    assert _locations(graph, created["uuid"])[0]["project"] == "alpha"

    _file(graph, "beta", "src/main.py")
    assert StructureGraphWriter(graph).query_blast_radius(
        "beta", ["src/main.py"]
    )["open_todos"] == []
    assert [row["uuid"] for row in StructureGraphWriter(graph).query_blast_radius(
        "alpha", ["src/main.py"]
    )["open_todos"]] == [created["uuid"]]


def test_unknown_and_conflicting_projects_never_fall_back(test_neo4j_repo: Any) -> None:
    graph = test_neo4j_repo
    _file(graph, "alpha", "src/main.py")
    repo = TodoRepository(graph)

    unknown = repo.create_todo(
        content="unknown", code_ref="src/main.py", structure_project="missing"
    )
    assert unknown["linked_files"] == []
    assert unknown["locations"][0]["unresolved_reason"] == "file_not_found"
    assert _edges(graph, unknown["uuid"]) == []

    conflict = repo.create_todo(
        content="conflict", code_ref="projects/org/alpha/src/main.py",
        structure_project="beta",
    )
    assert conflict["linked_files"] == []
    assert conflict["locations"][0]["unresolved_reason"] == "project_conflict"
    assert _edges(graph, conflict["uuid"]) == []


def test_duplicate_legacy_matches_refuse_even_with_project(test_neo4j_repo: Any) -> None:
    graph = test_neo4j_repo
    _file(graph, "alpha", "src/main.py")
    _file(graph, "alpha", "src/main.py")
    result = TodoRepository(graph).create_todo(
        content="duplicate", code_ref="src/main.py", structure_project="alpha"
    )
    assert result["locations"][0]["unresolved_reason"] == "ambiguous_file"
    assert _edges(graph, result["uuid"]) == []


def test_multiple_locations_resolve_independently_and_recheck_at_final_write(
    test_neo4j_repo: Any,
) -> None:
    graph = test_neo4j_repo
    _file(graph, "alpha", "src/a.py")
    _file(graph, "alpha", "src/b.py")
    _file(graph, "beta", "src/b.py")

    class AddCandidateBeforeMutation:
        def execute(self, query: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
            if "collect(DISTINCT f) AS candidates" in query:
                _file(graph, "beta", "src/a.py")
            return graph.execute(query, params)

    result = TodoRepository(AddCandidateBeforeMutation()).create_todo(
        content="two", code_ref="src/a.py:1, src/b.py:2"
    )
    assert result["linked_files"] == []
    assert [loc["unresolved_reason"] for loc in result["locations"]] == [
        "ambiguous_file", "ambiguous_file",
    ]
    assert _edges(graph, result["uuid"]) == []
    assert len(_locations(graph, result["uuid"])) == 2


def test_multiple_locations_can_mix_one_link_and_one_ambiguous_ref(
    test_neo4j_repo: Any,
) -> None:
    graph = test_neo4j_repo
    unique_uuid = _file(graph, "alpha", "src/unique.py")
    _file(graph, "alpha", "src/common.py")
    _file(graph, "beta", "src/common.py")
    result = TodoRepository(graph).create_todo(
        content="mixed", code_ref="src/unique.py:1, src/common.py:2"
    )
    assert result["linked_files"] == [
        {"ordinal": 0, "path": "src/unique.py", "project": "alpha"}
    ]
    assert [loc["resolution_status"] for loc in result["locations"]] == [
        "resolved", "unresolved",
    ]
    assert _edges(graph, result["uuid"]) == [
        {"uuid": unique_uuid, "project": "alpha", "path": "src/unique.py"}
    ]
    reread = TodoRepository(graph).get_todo(result["uuid"])
    assert reread is not None
    assert reread["linked_file_path"] == "src/unique.py"
    assert [loc["unresolved_reason"] for loc in reread["locations"]] == [
        None, "ambiguous_file",
    ]


def test_restart_read_returns_all_distinct_file_links_and_locations(
    test_neo4j_repo: Any,
) -> None:
    graph = test_neo4j_repo
    _file(graph, "alpha", "src/a.py")
    _file(graph, "alpha", "src/b.py")
    created = TodoRepository(graph).create_todo(
        content="two files", code_ref="src/a.py:1, src/b.py:2"
    )
    assert len(created["linked_files"]) == 2
    reread = TodoRepository(graph).get_todo(created["uuid"])
    assert reread is not None
    assert {item["structure_path"] for item in reread["linked_files"]} == {
        "src/a.py", "src/b.py",
    }
    assert [loc["path"] for loc in reread["locations"]] == ["src/a.py", "src/b.py"]
    assert len(_edges(graph, created["uuid"])) == 2


def test_namespace_cannot_link_foreign_file(test_neo4j_repo: Any) -> None:
    graph = test_neo4j_repo
    _file(graph, "alpha", "src/private.py", namespace="foreign")
    result = TodoRepository(graph).create_todo(
        content="private", code_ref="src/private.py", namespace="mine"
    )
    assert result["linked_files"] == []
    assert result["locations"][0]["unresolved_reason"] == "file_not_found"
    assert _edges(graph, result["uuid"]) == []

    shared_uuid = _file(graph, "alpha", "src/private.py")
    shared = TodoRepository(graph).create_todo(
        content="shared", code_ref="src/private.py", namespace="mine"
    )
    assert _edges(graph, shared["uuid"]) == [{
        "uuid": shared_uuid, "project": "alpha", "path": "src/private.py",
    }]
