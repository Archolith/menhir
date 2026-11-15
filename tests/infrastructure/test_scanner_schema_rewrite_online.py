"""A scanner-schema change rewrites stored symbols of files whose mtime did not change.

`write_project` rewrites symbols only for files whose mtime changed. When the scanner's rules
change what a symbol stores (v11 elides text literals from signatures), unchanged files would keep
the old signature forever, so a graph written by an older scanner must be rewritten once. Run
with ``pytest --run-online``; conftest forces the test instance and refuses production.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from menhir.infrastructure.project_scanner import SCANNER_SCHEMA_VERSION, ProjectScanner
from menhir.infrastructure.structure_queries import StructureGraphWriter

pytestmark = [pytest.mark.online]


class _Repo:
    def __init__(self, driver):
        self._driver = driver

    def execute(self, query, params=None, **_kw):
        with self._driver.session() as session:
            return [dict(r) for r in session.run(query, params or {})]


@pytest.fixture
def repo():
    from neo4j import GraphDatabase

    uri = os.getenv("MENHIR_TEST_NEO4J_URI", "bolt://127.0.0.1:7688")
    user = os.getenv("MENHIR_TEST_NEO4J_USER", "neo4j")
    password = os.getenv("MENHIR_TEST_NEO4J_PASSWORD", "testpassword")
    driver = GraphDatabase.driver(uri, auth=(user, password))
    yield _Repo(driver)
    driver.close()


@pytest.fixture
def name(repo):
    project = f"schema-rewrite-{uuid.uuid4().hex[:10]}"
    yield project
    repo.execute("MATCH (n:Entity {structure_project: $p}) DETACH DELETE n", {"p": project})


def _scan(root: Path, name: str):
    scan = ProjectScanner().scan(root, name=name)
    scan.project_id = f"pid-{name}"
    return scan


def _signature(repo, name: str) -> str:
    rows = repo.execute(
        "MATCH (s:Entity {structure_project: $p, structure_role: 'symbol', name: 'connect'}) "
        "RETURN s.symbol_signature AS sig",
        {"p": name},
    )
    assert len(rows) == 1, rows
    return rows[0]["sig"]


def _write(repo, scan) -> None:
    StructureGraphWriter(repo).write_project(scan, "s", "u")


def _plant_old_graph(repo, name: str, *, schema: int | None) -> None:
    """Make the stored graph look like an older scanner wrote it."""
    repo.execute(
        "MATCH (s:Entity {structure_project: $p, structure_role: 'symbol', name: 'connect'}) "
        "SET s.symbol_signature = \"def connect(password='hunter2')\"",
        {"p": name},
    )
    repo.execute(
        "MATCH (p:Entity {structure_project: $p, structure_role: 'project'}) "
        "SET p.scanner_schema = $schema",
        {"p": name, "schema": schema},
    )


def test_unchanged_file_is_rewritten_after_a_schema_change(repo, name, tmp_path) -> None:
    (tmp_path / "db.py").write_text("def connect(password='hunter2'):\n    pass\n", encoding="utf-8")
    scan = _scan(tmp_path, name)
    _write(repo, scan)
    assert "hunter2" not in _signature(repo, name)

    # A graph from before v11: old signature stored, no recorded schema. Same file, same mtime.
    _plant_old_graph(repo, name, schema=None)
    _write(repo, _scan(tmp_path, name))
    assert "hunter2" not in _signature(repo, name)
    rows = repo.execute(
        "MATCH (p:Entity {structure_project: $p, structure_role: 'project'}) "
        "RETURN p.scanner_schema AS s",
        {"p": name},
    )
    assert rows[0]["s"] == SCANNER_SCHEMA_VERSION


def test_current_schema_keeps_the_incremental_skip(repo, name, tmp_path) -> None:
    (tmp_path / "db.py").write_text("def connect(password='hunter2'):\n    pass\n", encoding="utf-8")
    _write(repo, _scan(tmp_path, name))

    # Same schema, same mtime: the writer must still skip the unchanged file (control).
    _plant_old_graph(repo, name, schema=SCANNER_SCHEMA_VERSION)
    _write(repo, _scan(tmp_path, name))
    assert "hunter2" in _signature(repo, name)
