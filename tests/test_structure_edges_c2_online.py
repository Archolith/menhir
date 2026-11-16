"""#680 against a real Neo4j: a rescan replaces derived edges instead of accumulating them.

Each case writes a real scan, changes the project on disk, rescans and writes again, then reads
the edges back. Run with ``pytest --run-online``; conftest forces the test instance.
"""

from __future__ import annotations

import os
import time
import uuid
from pathlib import Path

import pytest

from menhir.infrastructure import project_scanner
from menhir.infrastructure.project_scanner import ProjectScanner
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
    project = f"c2-edges-{uuid.uuid4().hex[:10]}"
    yield project
    repo.execute("MATCH (n:Entity {structure_project: $p}) DETACH DELETE n", {"p": project})


def _write_file(root: Path, rel: str, content: str = "") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    # A distinct mtime, so the writer's incremental diff sees the edit.
    stamp = time.time() + 10
    os.utime(path, (stamp, stamp))


def _write(repo, root: Path, name: str, *, project_id: str | None = None) -> None:
    scan = ProjectScanner().scan(root, name=name)
    scan.project_id = project_id or f"pid-{name}"
    StructureGraphWriter(repo).write_project(scan, "s", "u")


def _edges(repo, name: str, rel: str) -> set[tuple[str, str]]:
    rows = repo.execute(
        f"MATCH (a:Entity {{structure_project: $p}})-[:{rel}]->(b:Entity) "
        "RETURN a.structure_path AS a, b.structure_path AS b",
        {"p": name},
    )
    return {(r["a"], r["b"]) for r in rows}


def _base(root: Path) -> None:
    _write_file(root, "pyproject.toml", "[project]\nname='p'\n")
    _write_file(root, "db.py", "def connect():\n    pass\n")
    _write_file(root, "cache.py", "")
    _write_file(root, "app.py", "import db\nimport cache\n")


def test_a_removed_import_is_gone_after_rescan(repo, name, tmp_path) -> None:
    _base(tmp_path)
    _write(repo, tmp_path, name)
    assert {("app.py", "db.py"), ("app.py", "cache.py")} <= _edges(repo, name, "IMPORTS")

    _write_file(tmp_path, "app.py", "import db\n")
    _write(repo, tmp_path, name)

    edges = _edges(repo, name, "IMPORTS")
    assert ("app.py", "db.py") in edges
    assert ("app.py", "cache.py") not in edges


def test_a_files_last_import_is_removed_too(repo, name, tmp_path) -> None:
    """The file then has no edges in the scan at all, so it is absent from the edge list."""
    _base(tmp_path)
    _write(repo, tmp_path, name)

    _write_file(tmp_path, "app.py", "x = 1\n")
    _write(repo, tmp_path, name)

    assert not {e for e in _edges(repo, name, "IMPORTS") if e[0] == "app.py"}


def test_a_test_link_moves_when_a_better_source_appears(repo, name, tmp_path) -> None:
    _write_file(tmp_path, "pyproject.toml", "[project]\nname='p'\n")
    _write_file(tmp_path, "a/models.py")
    _write_file(tmp_path, "tests/db/test_models.py", "x = 1\n")
    _write(repo, tmp_path, name)
    assert _edges(repo, name, "TESTS") == {("tests/db/test_models.py", "a/models.py")}

    _write_file(tmp_path, "src/db/models.py")
    _write(repo, tmp_path, name)

    assert _edges(repo, name, "TESTS") == {("tests/db/test_models.py", "src/db/models.py")}


def test_a_dropped_import_is_not_kept_alive_by_a_tests_edge(repo, name, tmp_path) -> None:
    _write_file(tmp_path, "pyproject.toml", "[project]\nname='p'\n")
    _write_file(tmp_path, "db.py")
    _write_file(tmp_path, "tests/test_db.py", "import db\n")
    _write(repo, tmp_path, name)
    assert ("tests/test_db.py", "db.py") in _edges(repo, name, "IMPORTS")

    _write_file(tmp_path, "tests/test_db.py", "x = 1\n")
    _write(repo, tmp_path, name)

    assert ("tests/test_db.py", "db.py") in _edges(repo, name, "TESTS")
    assert ("tests/test_db.py", "db.py") not in _edges(repo, name, "IMPORTS")


def test_another_project_with_the_same_paths_keeps_its_edges(repo, name, tmp_path) -> None:
    """The prune walks from paths; only the owner arms keep it inside this project.

    (Two identities under ONE display name share nodes, because entity MERGE is keyed by name
    and path -- the pre-existing #99 residue, not something this prune can separate.)
    """
    other_name = f"{name}-other"
    other = tmp_path / "other"
    mine = tmp_path / "mine"
    _base(other)
    _base(mine)
    try:
        _write(repo, other, other_name)
        _write(repo, mine, name)

        _write_file(mine, "app.py", "import db\n")
        _write(repo, mine, name)

        assert ("app.py", "cache.py") not in _edges(repo, name, "IMPORTS")
        assert ("app.py", "cache.py") in _edges(repo, other_name, "IMPORTS")
    finally:
        repo.execute(
            "MATCH (n:Entity {structure_project: $p}) DETACH DELETE n", {"p": other_name}
        )


def test_a_capped_scan_keeps_edges_into_files_the_cap_dropped(
    repo, name, tmp_path, monkeypatch
) -> None:
    _write_file(tmp_path, "pyproject.toml", "[project]\nname='p'\n")
    _write_file(tmp_path, "app.py", "from tests import helper\nimport db\n")
    _write_file(tmp_path, "db.py")
    _write_file(tmp_path, "tests/__init__.py")
    _write_file(tmp_path, "tests/helper.py")
    _write(repo, tmp_path, name)
    assert ("app.py", "tests/helper.py") in _edges(repo, name, "IMPORTS")

    # The cap drops tests first: tests/helper.py leaves the scan but not the graph.
    monkeypatch.setattr(project_scanner, "_MAX_KEY_FILES", 3)
    _write_file(tmp_path, "app.py", "from tests import helper\n")
    _write(repo, tmp_path, name)

    edges = _edges(repo, name, "IMPORTS")
    assert ("app.py", "tests/helper.py") in edges  # not evidence of removal
    assert ("app.py", "db.py") not in edges  # both ends scanned: really removed
