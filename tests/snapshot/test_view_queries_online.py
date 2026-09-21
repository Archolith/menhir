from __future__ import annotations

import os
import uuid

import pytest

from menhir.snapshot.canonical_view import CANONICAL_VIEW_CONSTRAINTS, publish_root
from menhir.snapshot.view_queries import SNAPSHOT_STATUS_KEY, SnapshotStructureViewReader
from menhir.snapshot.view_root import (
    VIEW_ROOT_CONSTRAINTS,
    begin_root,
    complete_root,
)

pytestmark = [pytest.mark.online, pytest.mark.timeout(120)]

VIEW = "canonical"


class _Repo:
    def __init__(self, driver) -> None:
        self._driver = driver

    def execute(self, statement: str, params: dict):
        with self._driver.session() as session:
            return [record.data() for record in session.run(statement, **params)]


@pytest.fixture
def repo():
    from neo4j import GraphDatabase

    uri = os.getenv("MENHIR_TEST_NEO4J_URI", "bolt://127.0.0.1:7688")
    user = os.getenv("MENHIR_TEST_NEO4J_USER", "neo4j")
    password = os.getenv("MENHIR_TEST_NEO4J_PASSWORD", "testpassword")
    driver = GraphDatabase.driver(uri, auth=(user, password))
    value = _Repo(driver)
    for statement in [*CANONICAL_VIEW_CONSTRAINTS, *VIEW_ROOT_CONSTRAINTS]:
        value.execute(statement, {})
    try:
        yield value
    finally:
        value.execute(
            "MATCH (v:CanonicalView) WHERE v.project_id STARTS WITH 'view-read-test-' "
            "DETACH DELETE v",
            {},
        )
        value.execute(
            "MATCH (n:SnapshotEntity) WHERE n.project_id STARTS WITH 'view-read-test-' "
            "DETACH DELETE n",
            {},
        )
        value.execute(
            "MATCH (r:ViewRoot) WHERE r.project_id STARTS WITH 'view-read-test-' "
            "DETACH DELETE r",
            {},
        )
        driver.close()


def _published_graph(repo):
    project_id = f"view-read-test-{uuid.uuid4().hex[:8]}"
    root = begin_root(
        repo, project_id=project_id, view_key=VIEW, snapshot_id="snapshot-online"
    )
    rows = [
        {
            "path": ".",
            "role": "project",
            "name": "display-online",
            "content": "Snapshot project",
            "extra": {
                "stack": "python",
                "files_discovered": 3,
                "files_eligible": 3,
                "files_indexed": 3,
                "partial_index": False,
            },
        },
        {"path": "src/a.py", "role": "file", "name": "a.py", "content": "A"},
        {"path": "src/b.py", "role": "file", "name": "b.py", "content": "B"},
        {
            "path": "tests/test_a.py",
            "role": "test",
            "name": "test_a.py",
            "content": "tests",
        },
        {"path": "dep:neo4j", "role": "dependency", "name": "neo4j", "content": ""},
        {
            "path": "src/a.py::run",
            "role": "symbol",
            "name": "run",
            "content": "Run.",
            "extra": {
                "symbol_kind": "function",
                "symbol_line": 1,
                "symbol_signature": "run()",
                "symbol_parent": "",
                "symbol_decorator": "",
            },
        },
        {
            "path": "src/b.py::call_run",
            "role": "symbol",
            "name": "call_run",
            "content": "Call run.",
            "extra": {
                "symbol_kind": "function",
                "symbol_line": 1,
                "symbol_signature": "call_run()",
                "symbol_parent": "",
                "symbol_decorator": "",
            },
        },
        {
            "path": "endpoint:src/a.py:run",
            "role": "endpoint",
            "name": "run",
            "content": "Run endpoint",
        },
    ]
    repo.execute(
        "UNWIND $rows AS row "
        "CREATE (n:SnapshotEntity {view_root: $root, project_id: $pid, "
        "structure_path: row.path, structure_role: row.role, name: row.name, "
        "content: row.content}) SET n += coalesce(row.extra, {})",
        {"rows": rows, "root": root.root_id, "pid": project_id},
    )
    for relationship, source, target in [
        ("IMPORTS", "src/b.py", "src/a.py"),
        ("TESTS", "tests/test_a.py", "src/a.py"),
        ("TESTS", "tests/test_a.py", "src/b.py"),
        ("DEFINES", "src/a.py", "src/a.py::run"),
        ("DEFINES", "src/b.py", "src/b.py::call_run"),
        ("CALLS", "src/b.py::call_run", "src/a.py::run"),
        ("EXPOSES", "src/a.py", "endpoint:src/a.py:run"),
    ]:
        repo.execute(
            "MATCH (a:SnapshotEntity {view_root: $root, project_id: $pid, "
            "structure_path: $source}) "
            "MATCH (b:SnapshotEntity {view_root: $root, project_id: $pid, "
            "structure_path: $target}) CREATE (a)-[r:" + relationship + "]->(b)",
            {
                "root": root.root_id,
                "pid": project_id,
                "source": source,
                "target": target,
            },
        )

    repo.execute(
        "CREATE (wrong:SnapshotEntity {view_root: 'wrong-root', project_id: $pid, "
        "structure_path: 'wrong.py', structure_role: 'file'}) "
        "WITH wrong MATCH (current:SnapshotEntity {view_root: $root, project_id: $pid, "
        "structure_path: 'src/a.py'}) CREATE (wrong)-[:IMPORTS]->(current)",
        {"pid": project_id, "root": root.root_id},
    )
    complete_root(repo, root_id=root.root_id)
    publish_root(
        repo,
        project_id=project_id,
        view_key=VIEW,
        root_id=root.root_id,
        expected_generation=0,
        actor="test:operator",
    )
    repo.execute(
        "MATCH (v:CanonicalView {project_id: $pid, view_key: $view_key}) "
        "SET v.display_name = 'display-online'",
        {"pid": project_id, "view_key": VIEW},
    )
    return project_id


def test_published_snapshot_serves_all_structure_queries_and_rejects_wrong_root_edges(
    repo,
) -> None:
    project_id = _published_graph(repo)
    reader = SnapshotStructureViewReader(repo)

    overview = reader.query(project_id, "overview")
    files = reader.query(project_id, "files")
    imports = reader.query(project_id, "imports", file_path="src/a.py")
    tests = reader.query(project_id, "tests")
    endpoints = reader.query(project_id, "endpoints")
    dependencies = reader.query(project_id, "dependencies")
    cross_refs = reader.query(project_id, "cross_refs")
    radius = reader.query(project_id, "blast_radius", file_paths=["src/a.py"])
    affected = reader.query(project_id, "affected_tests", file_paths=["src/a.py"])
    symbols = reader.query(project_id, "symbols", path="src/a.py")
    context = reader.query(project_id, "context", path="src/a.py")

    assert overview["data"]["stack"] == "python"
    assert [row["path"] for row in files["data"]] == [
        "src/a.py",
        "src/b.py",
        "tests/test_a.py",
    ]
    assert imports["data"] == {"imports": [], "imported_by": ["src/b.py"]}
    assert len(tests["data"]) == 2
    assert endpoints["data"][0]["name"] == "run"
    assert dependencies["data"] == ["neo4j"]
    assert cross_refs["data"] == []
    assert radius["data"]["directly_affected"] == ["src/b.py"]
    assert radius["data"]["function_callers"][0]["caller"] == "call_run"
    assert affected["data"]["test_files"] == ["tests/test_a.py"]
    assert symbols["data"]["symbols"][0]["name"] == "run"
    assert context["data"]["tested_by"] == ["tests/test_a.py"]
    assert overview[SNAPSHOT_STATUS_KEY]["snapshot_id"] == "snapshot-online"


def test_display_name_resolution_and_degraded_read_status(repo) -> None:
    project_id = _published_graph(repo)
    repo.execute(
        "MATCH (v:CanonicalView {project_id: $pid, view_key: $view_key}) "
        "SET v.degraded = true, v.degraded_reason = 'operator detail'",
        {"pid": project_id, "view_key": VIEW},
    )

    result = SnapshotStructureViewReader(repo).query("display-online", "dependencies")

    assert result["data"] == ["neo4j"]
    assert result[SNAPSHOT_STATUS_KEY]["project_id"] == project_id
    assert result[SNAPSHOT_STATUS_KEY]["degraded"] is True
