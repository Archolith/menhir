"""P4: the writer that fills a root, scanned from a real tree by the real scanner.

Design: `.agent/plans/menhir-snapshot-p4-graph-write-design-2026-09-17.md`.

Two properties matter more than the rest and both are counterexamples rather than happy paths:

* **A snapshot write cannot touch local scanning.** The owner's constraint on this whole phase, and
  the reason snapshot nodes carry a different label. Asserted by running the local path's own
  matching patterns against a graph that holds a snapshot.
* **A root is self-contained.** Every edge stays inside its own root, so reclaiming one root can
  never corrupt another -- which is what makes `purge_root` safe to run at all.

The scan comes from the REAL `ProjectScanner` over a real temporary tree. A hand-built scan result
would let the writer and the test agree on a shape neither shares with the scanner.
"""

from __future__ import annotations

import os
import uuid

import pytest

from menhir.snapshot.canonical_view import CANONICAL_VIEW_CONSTRAINTS, read_view
from menhir.snapshot.promotion import promote_snapshot
from menhir.snapshot.promotion_attempt import PROMOTION_ATTEMPT_CONSTRAINTS
from menhir.snapshot.snapshot_structure import (
    iter_root_paths,
    snapshot_structure_writer,
    write_snapshot_structure,
)
from menhir.snapshot.view_root import (
    SNAPSHOT_NODE_LABEL,
    VIEW_ROOT_CONSTRAINTS,
    VIEW_ROOT_PROPERTY,
    begin_root,
    purge_root,
    retire_root,
)

pytestmark = [pytest.mark.online, pytest.mark.timeout(180)]

VIEW = "canonical"


class _Repo:
    def __init__(self, driver) -> None:
        self.driver = driver

    def execute(self, statement: str, params: dict):
        with self.driver.session() as session:
            return [record.data() for record in session.run(statement, **params)]


@pytest.fixture
def repo():
    from neo4j import GraphDatabase

    uri = os.getenv("MENHIR_TEST_NEO4J_URI", "bolt://127.0.0.1:7688")
    user = os.getenv("MENHIR_TEST_NEO4J_USER", "neo4j")
    password = os.getenv("MENHIR_TEST_NEO4J_PASSWORD", "testpassword")
    driver = GraphDatabase.driver(uri, auth=(user, password))
    r = _Repo(driver)
    for statement in [
        *CANONICAL_VIEW_CONSTRAINTS,
        *VIEW_ROOT_CONSTRAINTS,
        *PROMOTION_ATTEMPT_CONSTRAINTS,
    ]:
        r.execute(statement, {})

    def _clear_decoys() -> None:
        # Both before and after. The local-scan test counts real `:Entity` rows, so a decoy left
        # behind by an earlier failing run makes the next run fail for the wrong reason -- which is
        # exactly what happened once. Scoped to the p4-test prefix, so it can only remove nodes
        # these tests created.
        r.execute(
            "MATCH (n:Entity) WHERE n.structure_project_id STARTS WITH 'p4-test-' "
            "DETACH DELETE n",
            {},
        )

    _clear_decoys()
    try:
        yield r
    finally:
        _clear_decoys()
        r.execute(
            "MATCH (v:CanonicalView) WHERE v.project_id STARTS WITH 'p4-test-' DETACH DELETE v", {}
        )
        r.execute(
            f"MATCH (n:{SNAPSHOT_NODE_LABEL}) WHERE n.project_id STARTS WITH 'p4-test-' "
            "DETACH DELETE n",
            {},
        )
        r.execute(
            "MATCH (r:ViewRoot) WHERE r.project_id STARTS WITH 'p4-test-' DETACH DELETE r", {}
        )
        r.execute(
            "MATCH (a:SnapshotPromotionAttempt) "
            "WHERE a.project_id STARTS WITH 'p4-test-' DETACH DELETE a",
            {},
        )
        driver.close()


@pytest.fixture
def pid():
    return f"p4-test-{uuid.uuid4().hex[:8]}"


def _project(tmp_path, name: str, files: dict[str, str]):
    """Write a small real project and scan it with the real scanner."""
    from menhir.infrastructure.project_scanner import ProjectScanner

    root = tmp_path / name
    for rel, body in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    return ProjectScanner().scan(root)


TWO_FILE_PROJECT = {
    "pyproject.toml": "[project]\nname = 'demo'\ndependencies = ['requests']\n",
    "src/app.py": "import helpers\n\n\ndef run(x: int) -> int:\n    '''Run it.'''\n    return x\n",
    "src/helpers.py": "def helper() -> str:\n    '''Help.'''\n    return 'ok'\n",
    "tests/test_app.py": "import app\n\n\ndef test_run():\n    assert app.run(1) == 1\n",
}


@pytest.fixture
def scan(tmp_path):
    return _project(tmp_path, "demo", TWO_FILE_PROJECT)


def _root(repo, pid, snapshot_id="snap-1"):
    return begin_root(
        repo, project_id=pid, view_key=VIEW, snapshot_id=snapshot_id
    ).root_id


# --- the constraint the owner set on this whole phase ------------------------------------------------


def test_a_snapshot_write_is_invisible_to_every_local_scan_query(repo, pid, scan) -> None:
    """THE test for this module. Local scanning must not be affected by any of this work.

    Every local read and prune in `structure_queries` matches `(n:Entity ...)`. If snapshot nodes
    carried that label, a local scan of a project with the same display name could MERGE onto them,
    and `_delete_stale_role_entities_multi` could delete them for not appearing in a local scan --
    #99's failure with a new cause.

    So this asserts the separation the way the local path would encounter it: its own matching
    patterns, run against a graph that holds a full snapshot.

    **A decoy local node is planted first, and that is not decoration.** An earlier version of this
    test asserted only that those patterns matched zero rows -- which was true because the test
    database happens to hold no `:Entity` nodes at all, so it would have passed no matter what
    label the writer used. The decoy makes the assertion discriminating: the local patterns must
    find it, and find NOTHING else.
    """
    root_id = _root(repo, pid)
    write_snapshot_structure(repo, scan, root_id=root_id, project_id=pid)
    assert iter_root_paths(repo, root_id=root_id), "the snapshot must actually be there"

    decoy_path = next(f.rel_path for f in scan.files)
    decoy_name = f"decoy-{pid}"
    repo.execute(
        "CREATE (n:Entity {structure_project: $name, structure_project_id: $pid, "
        "  structure_path: $path, structure_role: 'file', name: $decoy})",
        {"name": scan.name, "pid": pid, "path": decoy_path, "decoy": decoy_name},
    )

    by_name = repo.execute(
        "MATCH (n:Entity) WHERE n.structure_project_id = $pid RETURN n.name AS name",
        {"pid": pid},
    )
    assert [row["name"] for row in by_name] == [decoy_name], (
        "a local read scoped to this project must see only the local node"
    )

    # The exact shape of the local stale-role prune: everything with a file-ish role whose path the
    # local scan did not see. With the snapshot sharing a label, this would delete the snapshot.
    #
    # Narrowed to THIS scan's paths rather than the whole database -- a global count is disturbed
    # by any other test in the session, which is how a first version of this failed in the suite
    # and passed alone. It is deliberately NOT narrowed by `structure_project`, because snapshot
    # nodes do not carry that property: filtering on it would exclude them by accident and make
    # the assertion vacuous all over again.
    prunable = repo.execute(
        "MATCH (n:Entity) WHERE n.structure_role IN ['file', 'entrypoint', 'config', 'test'] "
        "AND n.structure_path IN $paths RETURN count(n) AS c",
        {"paths": [f.rel_path for f in scan.files]},
    )
    assert prunable[0]["c"] == 1, (
        "the local prune's pattern must reach exactly the local node and no snapshot node"
    )


def test_snapshot_nodes_do_not_carry_the_entity_label_at_all(repo, pid, scan) -> None:
    """Separation by construction, not by a filter someone has to remember to add.

    A label check rather than a query check, because this is what makes the property hold for
    queries that have not been written yet.
    """
    root_id = _root(repo, pid)
    write_snapshot_structure(repo, scan, root_id=root_id, project_id=pid)

    rows = repo.execute(
        f"MATCH (n:{SNAPSHOT_NODE_LABEL}) WHERE n.{VIEW_ROOT_PROPERTY} = $root "
        "RETURN labels(n) AS labels",
        {"root": root_id},
    )
    assert rows
    for row in rows:
        assert row["labels"] == [SNAPSHOT_NODE_LABEL]


# --- what the writer produces ----------------------------------------------------------------------


def test_the_root_holds_what_the_scanner_found(repo, pid, scan) -> None:
    """Faithfulness to the scan, asserted against the scan rather than a hand-written list."""
    root_id = _root(repo, pid)
    # Guards against the whole assertion being vacuous if the scanner ever stops finding this tree.
    assert {f.rel_path for f in scan.files} >= {"src/app.py", "src/helpers.py"}
    assert scan.symbols

    report = write_snapshot_structure(repo, scan, root_id=root_id, project_id=pid)

    files = set(iter_root_paths(repo, root_id=root_id, role="file"))
    files |= set(iter_root_paths(repo, root_id=root_id, role="test"))
    files |= set(iter_root_paths(repo, root_id=root_id, role="config"))
    files |= set(iter_root_paths(repo, root_id=root_id, role="entrypoint"))
    assert files == {f.rel_path for f in scan.files}

    symbols = iter_root_paths(repo, root_id=root_id, role="symbol")
    assert len(symbols) == len(scan.symbols)
    assert report["nodes"] > 0


def test_two_roots_from_different_snapshots_do_not_share_nodes(repo, pid, tmp_path) -> None:
    """Roots are parallel copies. If they shared nodes, reclaiming one would gut the other."""
    first = _project(tmp_path, "one", TWO_FILE_PROJECT)
    shrunk = dict(TWO_FILE_PROJECT)
    del shrunk["src/helpers.py"]
    second = _project(tmp_path, "two", shrunk)

    root_a = _root(repo, pid, "snap-a")
    root_b = _root(repo, pid, "snap-b")
    write_snapshot_structure(repo, first, root_id=root_a, project_id=pid)
    write_snapshot_structure(repo, second, root_id=root_b, project_id=pid)

    shared = repo.execute(
        f"MATCH (n:{SNAPSHOT_NODE_LABEL}) WHERE n.{VIEW_ROOT_PROPERTY} IN [$a, $b] "
        "RETURN n.structure_path AS path, count(DISTINCT n) AS copies "
        "ORDER BY path",
        {"a": root_a, "b": root_b},
    )
    overlapping = [row for row in shared if row["path"] == "src/app.py"]
    assert overlapping and overlapping[0]["copies"] == 2, (
        "a path in both snapshots must exist once per root, not be shared between them"
    )


def test_every_edge_stays_inside_its_own_root(repo, pid, tmp_path) -> None:
    """A cross-root edge would make a reclaimed root corrupt the one that outlived it.

    The writer matches both endpoints within the root for exactly this reason, and this is the
    assertion that keeps it true if that MATCH is ever loosened.
    """
    first = _project(tmp_path, "one", TWO_FILE_PROJECT)
    second = _project(tmp_path, "two", TWO_FILE_PROJECT)
    root_a = _root(repo, pid, "snap-a")
    root_b = _root(repo, pid, "snap-b")
    write_snapshot_structure(repo, first, root_id=root_a, project_id=pid)
    write_snapshot_structure(repo, second, root_id=root_b, project_id=pid)

    crossing = repo.execute(
        f"MATCH (s:{SNAPSHOT_NODE_LABEL})-[]->(t:{SNAPSHOT_NODE_LABEL}) "
        f"WHERE s.{VIEW_ROOT_PROPERTY} <> t.{VIEW_ROOT_PROPERTY} RETURN count(*) AS c",
        {},
    )
    assert crossing[0]["c"] == 0


def test_the_writer_renews_the_lease_between_batches(repo, pid, scan) -> None:
    """A lease checked once before a write of this size expires in the middle of it."""
    root_id = _root(repo, pid)
    renewals: list[int] = []

    write_snapshot_structure(
        repo,
        scan,
        root_id=root_id,
        project_id=pid,
        batch_size=2,
        renew=lambda: renewals.append(1),
    )

    assert len(renewals) > 1, "a batched write must renew more than once"


def test_a_batched_write_produces_the_same_root_as_an_unbatched_one(repo, pid, tmp_path) -> None:
    """Batching is a lock-duration concern and must not change the result."""
    one = _project(tmp_path, "one", TWO_FILE_PROJECT)
    two = _project(tmp_path, "two", TWO_FILE_PROJECT)
    big = _root(repo, pid, "snap-big")
    small = _root(repo, pid, "snap-small")

    whole = write_snapshot_structure(repo, one, root_id=big, project_id=pid, batch_size=10_000)
    pieces = write_snapshot_structure(repo, two, root_id=small, project_id=pid, batch_size=2)

    assert whole["nodes"] == pieces["nodes"]
    assert whole["edges"] == pieces["edges"]
    assert iter_root_paths(repo, root_id=big) == iter_root_paths(repo, root_id=small)


# --- the contract with the purge ---------------------------------------------------------------------


def test_the_purge_reclaims_everything_the_writer_wrote(repo, pid, scan) -> None:
    """The label-and-property contract between the two modules, asserted rather than assumed.

    A writer using a different label would leave nodes no purge could ever find, and the graph
    would grow by a whole project structure on every promotion with nothing reporting it.
    """
    root_id = begin_root(
        repo, project_id=pid, view_key=VIEW, snapshot_id="snap-1", lease_seconds=0
    ).root_id
    report = write_snapshot_structure(repo, scan, root_id=root_id, project_id=pid)
    assert retire_root(repo, root_id=root_id) is True

    removed = purge_root(repo, root_id=root_id)

    assert removed == report["nodes"]
    assert iter_root_paths(repo, root_id=root_id) == []


# --- end to end --------------------------------------------------------------------------------------


def test_a_real_snapshot_promotes_end_to_end(repo, pid, scan) -> None:
    """The whole P4 path with nothing stubbed: scan, build, complete, flip."""
    report: dict = {}

    outcome = promote_snapshot(
        repo,
        project_id=pid,
        view_key=VIEW,
        snapshot_id="snap-1",
        actor="test:operator",
        write_structure=snapshot_structure_writer(repo, scan, project_id=pid, report=report),
    )

    view = read_view(repo, project_id=pid, view_key=VIEW)
    assert view.current_root == outcome.root_id
    assert report["nodes"] > 0
    assert set(iter_root_paths(repo, root_id=view.current_root, role="file")) <= {
        f.rel_path for f in scan.files
    }


def test_a_deleted_file_is_absent_from_the_next_root_and_present_in_the_previous(
    repo, pid, tmp_path
) -> None:
    """Deletion end to end, and the reason no prune was needed to implement it.

    The second snapshot simply does not mention the removed file, so the second root does not
    contain it. Nothing matched it by name; nothing deleted it. And the previous root still holds
    it, so the undo the gate requires is real.
    """
    before = _project(tmp_path, "before", TWO_FILE_PROJECT)
    shrunk = dict(TWO_FILE_PROJECT)
    del shrunk["src/helpers.py"]
    after = _project(tmp_path, "after", shrunk)

    first = promote_snapshot(
        repo,
        project_id=pid,
        view_key=VIEW,
        snapshot_id="snap-1",
        actor="test:operator",
        write_structure=snapshot_structure_writer(repo, before, project_id=pid),
    )
    second = promote_snapshot(
        repo,
        project_id=pid,
        view_key=VIEW,
        snapshot_id="snap-2",
        actor="test:operator",
        write_structure=snapshot_structure_writer(repo, after, project_id=pid),
    )

    assert "src/helpers.py" in iter_root_paths(repo, root_id=first.root_id)
    assert "src/helpers.py" not in iter_root_paths(repo, root_id=second.root_id)
    view = read_view(repo, project_id=pid, view_key=VIEW)
    assert view.current_root == second.root_id
    assert view.previous_root == first.root_id
