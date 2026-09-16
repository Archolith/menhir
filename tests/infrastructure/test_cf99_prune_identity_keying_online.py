"""#99 -- what the prune predicate actually deletes, against a REAL Neo4j.

The offline half asserts that the writer EMITS an identity-keyed predicate. It cannot assert that
the predicate selects the right nodes, because the recorder never executes anything: a mutation
turning `structure_project_id IS NULL AND ...` back into a bare name match passes every offline
test in this repo. The three properties below are the ones that make #99 fixed rather than
described, and each is a counterexample to the old behaviour rather than a restatement of the new
code:

1. a scan must not delete rows belonging to a DIFFERENT identity that shares its display name;
2. a renamed project must still find and prune its own rows;
3. pre-CF-257 rows, which carry no identity stamp at all, must still be reachable.

Run with ``pytest --run-online``; conftest forces the test instance (:7688) and refuses to run if
that resolves to production.
"""

from __future__ import annotations

import os
import uuid

import pytest

from menhir.infrastructure.structure_queries import StructureGraphWriter

pytestmark = [pytest.mark.online]


class _Repo:
    """Minimal `execute` over a driver session, matching Neo4jRepository's surface."""

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
    r = _Repo(driver)
    yield r
    driver.close()


@pytest.fixture
def tag():
    """Every node this module writes carries one tag, so cleanup can never touch anything else."""
    return f"cf99-{uuid.uuid4().hex[:10]}"


@pytest.fixture(autouse=True)
def _cleanup(repo, tag):
    yield
    repo.execute("MATCH (n:Entity) WHERE n.cf99_tag = $tag DETACH DELETE n", {"tag": tag})


def _seed(repo, tag, *, name, project_id, paths, role="endpoint"):
    """Write structure rows directly, so the test states the starting graph rather than deriving it."""
    repo.execute(
        """
        UNWIND $paths AS path
        CREATE (n:Entity {
            uuid: randomUUID(),
            cf99_tag: $tag,
            structure_project: $name,
            structure_project_id: $pid,
            structure_path: path,
            structure_role: $role,
            file_mtime: 100.0
        })
        """,
        {"tag": tag, "name": name, "pid": project_id, "paths": paths, "role": role},
    )


def _surviving(repo, tag, project_id):
    rows = repo.execute(
        """
        MATCH (n:Entity) WHERE n.cf99_tag = $tag
          AND coalesce(n.structure_project_id, '<null>') = $pid
        RETURN n.structure_path AS path ORDER BY path
        """,
        {"tag": tag, "pid": project_id if project_id is not None else "<null>"},
    )
    return [r["path"] for r in rows]


def test_a_prune_never_deletes_another_identitys_rows_under_the_same_name(repo, tag):
    """The property #99 exists for. Two identities, one display name -- the collision the old
    name-keyed predicate could not see, and the one a rename or a fork actually produces."""
    mine = str(uuid.uuid4())
    theirs = str(uuid.uuid4())
    _seed(repo, tag, name="shared-name", project_id=mine, paths=["endpoint:gone"])
    _seed(repo, tag, name="shared-name", project_id=theirs, paths=["endpoint:gone"])

    writer = StructureGraphWriter(neo4j=repo)
    deleted = writer._delete_stale_role_entities("shared-name", "endpoint", [], mine)

    assert deleted == 1
    assert _surviving(repo, tag, mine) == []
    assert _surviving(repo, tag, theirs) == ["endpoint:gone"], (
        "the other project's row was deleted by a scan that never named it"
    )


def test_a_renamed_project_still_prunes_its_own_rows(repo, tag):
    """Consequence 2 in the issue: post-rename the name-keyed prune matched nothing, so stale
    rows survived forever while the writer treated the project as never scanned."""
    pid = str(uuid.uuid4())
    _seed(repo, tag, name="old-name", project_id=pid, paths=["endpoint:stale", "endpoint:kept"])

    writer = StructureGraphWriter(neo4j=repo)
    deleted = writer._delete_stale_role_entities("new-name", "endpoint", ["endpoint:kept"], pid)

    assert deleted == 1
    assert _surviving(repo, tag, pid) == ["endpoint:kept"]


def test_a_renamed_project_reads_back_its_own_mtimes(repo, tag):
    """The same rename, one step earlier: `get_file_mtimes` returning {} is what sent the writer
    down the first-scan branch and orphaned every entity under the old name."""
    pid = str(uuid.uuid4())
    _seed(repo, tag, name="old-name", project_id=pid, paths=["src/main.py"], role="file")

    writer = StructureGraphWriter(neo4j=repo)

    assert writer.get_file_mtimes("new-name", pid) == {"src/main.py": 100.0}
    assert writer.get_file_mtimes("new-name", None) == {}, (
        "without an identity there is nothing to resolve the rename with; this is the "
        "documented residual, asserted so it cannot change silently"
    )


def test_legacy_rows_without_an_identity_stamp_are_still_reachable(repo, tag):
    """Rows written before CF-257 carry no `structure_project_id`. They are the reason the second
    arm exists: keyed on the id alone they would be unreachable and accumulate forever."""
    pid = str(uuid.uuid4())
    _seed(repo, tag, name="proj", project_id=None, paths=["endpoint:legacy"])
    _seed(repo, tag, name="proj", project_id=pid, paths=["endpoint:current"])

    writer = StructureGraphWriter(neo4j=repo)
    deleted = writer._delete_stale_role_entities("proj", "endpoint", [], pid)

    assert deleted == 2
    assert _surviving(repo, tag, None) == []
    assert _surviving(repo, tag, pid) == []


def test_legacy_arm_is_still_bounded_by_the_display_name(repo, tag):
    """The second arm must not become 'delete every unstamped row in the graph'."""
    pid = str(uuid.uuid4())
    _seed(repo, tag, name="proj", project_id=None, paths=["endpoint:mine"])
    _seed(repo, tag, name="somebody-else", project_id=None, paths=["endpoint:theirs"])

    writer = StructureGraphWriter(neo4j=repo)
    deleted = writer._delete_stale_role_entities("proj", "endpoint", [], pid)

    assert deleted == 1
    rows = repo.execute(
        "MATCH (n:Entity) WHERE n.cf99_tag = $tag RETURN n.structure_path AS path",
        {"tag": tag},
    )
    assert [r["path"] for r in rows] == ["endpoint:theirs"]


def test_symbol_full_replace_is_identity_scoped(repo, tag):
    """The instance the issue missed: a full symbol replace carries no path filter at all, so
    keyed on the name alone it emptied every same-named project's symbols on any forced scan."""
    mine = str(uuid.uuid4())
    theirs = str(uuid.uuid4())
    _seed(repo, tag, name="shared-name", project_id=mine, paths=["a.py::f"], role="symbol")
    _seed(repo, tag, name="shared-name", project_id=theirs, paths=["a.py::f"], role="symbol")

    writer = StructureGraphWriter(neo4j=repo)
    writer._write_symbols(
        [], [], "shared-name",
        project_id=mine, session_id="s1", user_id="u1", now="2026-09-16T00:00:00Z",
        changed_paths=None,
    )

    assert _surviving(repo, tag, mine) == []
    assert _surviving(repo, tag, theirs) == ["a.py::f"]
