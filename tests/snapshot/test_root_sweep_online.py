"""P4: the sweep that reclaims roots nothing can publish, without reclaiming one a reader needs.

Design: `.agent/plans/menhir-snapshot-p4-graph-write-design-2026-09-17.md`.

`retire_root` and `purge_root` were built and tested before anything called them, which meant a
failed promotion leaked a whole project structure permanently and the retention decision -- bounded
at roughly two structures per project -- quietly stopped being true. This is the pass that runs
them, and these are the failures it must not cause.

The dangerous mistake for a sweeper is not missing garbage; it is collecting something live. So
most of what follows asserts what the sweep LEAVES ALONE.
"""

from __future__ import annotations

import os
import uuid

import pytest

from menhir.snapshot.canonical_view import CANONICAL_VIEW_CONSTRAINTS, publish_root, read_view
from menhir.snapshot.promotion import PromotionError, promote_snapshot
from menhir.snapshot.snapshot_structure import iter_root_paths
from menhir.snapshot.view_root import (
    ROOT_ABANDONED,
    ROOT_BUILDING,
    ROOT_COMPLETE,
    SNAPSHOT_NODE_LABEL,
    VIEW_ROOT_CONSTRAINTS,
    VIEW_ROOT_PROPERTY,
    begin_root,
    complete_root,
    read_root,
    sweep_view_roots,
)

pytestmark = [pytest.mark.online, pytest.mark.timeout(120)]

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
    for statement in [*CANONICAL_VIEW_CONSTRAINTS, *VIEW_ROOT_CONSTRAINTS]:
        r.execute(statement, {})
    try:
        yield r
    finally:
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
        driver.close()


@pytest.fixture
def pid():
    return f"p4-test-{uuid.uuid4().hex[:8]}"


def _content(repo, pid, count=4):
    def _write(root_id: str, renew) -> None:
        repo.execute(
            f"UNWIND range(1, $n) AS i "
            f"CREATE (:{SNAPSHOT_NODE_LABEL} {{{VIEW_ROOT_PROPERTY}: $root, project_id: $pid, "
            f"  structure_path: 'f' + toString(i), structure_role: 'file'}})",
            {"n": count, "root": root_id, "pid": pid},
        )

    return _write


def _expire(repo, root_id: str) -> None:
    repo.execute(
        "MATCH (r:ViewRoot {root_id: $root}) SET r.lease_expires_at = 0", {"root": root_id}
    )


def _count(repo, root_id: str) -> int:
    rows = repo.execute(
        f"MATCH (n:{SNAPSHOT_NODE_LABEL}) WHERE n.{VIEW_ROOT_PROPERTY} = $root "
        "RETURN count(n) AS c",
        {"root": root_id},
    )
    return int(rows[0]["c"])


# --- what the sweep must leave alone ---------------------------------------------------------------


def test_the_sweep_does_not_touch_the_current_root(repo, pid) -> None:
    """The current root's lease WILL expire -- it is published, not being built.

    If expiry alone authorised collection, the first sweep after any promotion would delete the
    structure every reader is using.
    """
    outcome = promote_snapshot(
        repo,
        project_id=pid,
        view_key=VIEW,
        snapshot_id="snap-1",
        write_structure=_content(repo, pid),
    )
    _expire(repo, outcome.root_id)

    report = sweep_view_roots(repo, project_id=pid)

    assert report.purged_roots == 0
    assert _count(repo, outcome.root_id) == 4
    assert read_root(repo, root_id=outcome.root_id).state == ROOT_COMPLETE
    # The count matters as much as the outcome here. Safety is enforced twice below the sweep --
    # `retire_root` refuses a referenced root and `purge_root` refuses one that is not retired --
    # so a sweep that ignored the refusal would still not destroy anything. It would just report
    # having retired a root it did not, which is how a leak hides in a green dashboard.
    assert report.retired == 0, "a refused retirement must not be counted as one"


def test_the_sweep_does_not_touch_the_previous_root(repo, pid) -> None:
    """`previous` is the gate's escape hatch; collecting it removes the only undo."""
    first = promote_snapshot(
        repo, project_id=pid, view_key=VIEW, snapshot_id="snap-1",
        write_structure=_content(repo, pid),
    )
    second = promote_snapshot(
        repo, project_id=pid, view_key=VIEW, snapshot_id="snap-2",
        write_structure=_content(repo, pid),
    )
    _expire(repo, first.root_id)
    _expire(repo, second.root_id)

    report = sweep_view_roots(repo, project_id=pid)

    assert report.purged_roots == 0
    assert _count(repo, first.root_id) == 4
    view = read_view(repo, project_id=pid, view_key=VIEW)
    assert view.previous_root == first.root_id


def test_the_sweep_does_not_touch_a_build_still_in_progress(repo, pid) -> None:
    """Lease expiry is the evidence, and the only evidence. Not a PID, not elapsed wall time."""
    building = begin_root(repo, project_id=pid, view_key=VIEW, snapshot_id="snap-1")

    report = sweep_view_roots(repo, project_id=pid)

    assert report.retired == 0
    assert read_root(repo, root_id=building.root_id).state == ROOT_BUILDING


def test_the_sweep_does_not_reach_into_another_project(repo, pid) -> None:
    """A scoped sweep must stay scoped; the candidate query is the only place that could leak."""
    other = f"p4-test-{uuid.uuid4().hex[:8]}"
    stranger = begin_root(
        repo, project_id=other, view_key=VIEW, snapshot_id="snap-1", lease_seconds=0
    )

    sweep_view_roots(repo, project_id=pid)

    assert read_root(repo, root_id=stranger.root_id).state == ROOT_BUILDING


# --- what it must collect --------------------------------------------------------------------------


def test_the_sweep_reclaims_a_root_a_failed_promotion_left_behind(repo, pid) -> None:
    """The leak this exists to close.

    A failed write leaves a BUILDING root holding whatever it managed to write. Nothing references
    it and nothing ever will, so without a sweep it stays in the graph for the life of the
    deployment.
    """

    def _half_written(root_id: str, renew) -> None:
        _content(repo, pid)(root_id, renew)
        raise RuntimeError("died after writing some of it")

    with pytest.raises(PromotionError):
        promote_snapshot(
            repo,
            project_id=pid,
            view_key=VIEW,
            snapshot_id="snap-1",
            write_structure=_half_written,
            lease_seconds=0,
        )

    orphan = repo.execute(
        "MATCH (r:ViewRoot {project_id: $pid}) RETURN r.root_id AS root_id", {"pid": pid}
    )[0]["root_id"]
    assert _count(repo, orphan) == 4

    report = sweep_view_roots(repo, project_id=pid)

    assert report.retired == 1
    assert report.purged_roots == 1
    assert report.purged_nodes == 4
    assert _count(repo, orphan) == 0
    assert read_root(repo, root_id=orphan).state == ROOT_ABANDONED


def test_the_sweep_reclaims_a_root_demoted_past_previous(repo, pid) -> None:
    """Retention of exactly one, enforced rather than described.

    A third promotion drops the first root out of both slots. Until something collects it, "bounded
    at roughly two structures per project" is a claim with nothing behind it.
    """
    roots = [
        promote_snapshot(
            repo, project_id=pid, view_key=VIEW, snapshot_id=f"snap-{i}",
            write_structure=_content(repo, pid),
        ).root_id
        for i in range(3)
    ]
    for root_id in roots:
        _expire(repo, root_id)

    report = sweep_view_roots(repo, project_id=pid)

    assert report.purged_roots == 1, "only the demoted root may be collected"
    assert _count(repo, roots[0]) == 0
    assert _count(repo, roots[1]) == 4, "previous survives"
    assert _count(repo, roots[2]) == 4, "current survives"


def test_the_sweep_finishes_a_purge_that_died_halfway(repo, pid) -> None:
    """An interrupted purge must be resumable, or its remaining nodes are invisible garbage."""
    root_id = begin_root(
        repo, project_id=pid, view_key=VIEW, snapshot_id="snap-1", lease_seconds=0
    ).root_id
    _content(repo, pid, count=10)(root_id, lambda: None)
    sweep_view_roots(repo, project_id=pid)
    assert _count(repo, root_id) == 0

    # Re-add rows under the already-ABANDONED root, as a purge killed mid-batch would leave.
    _content(repo, pid, count=3)(root_id, lambda: None)
    assert read_root(repo, root_id=root_id).state == ROOT_ABANDONED

    report = sweep_view_roots(repo, project_id=pid)

    assert report.purged_nodes == 3
    assert _count(repo, root_id) == 0


def test_the_sweep_is_bounded_by_its_limit(repo, pid) -> None:
    """Maintenance should finish and run again rather than run forever."""
    for i in range(4):
        begin_root(
            repo, project_id=pid, view_key=VIEW, snapshot_id=f"snap-{i}", lease_seconds=0
        )

    report = sweep_view_roots(repo, project_id=pid, limit=2)

    assert report.examined == 2


def test_a_sweep_with_nothing_to_do_reports_nothing(repo, pid) -> None:
    """The ordinary case, and it must not be confused with a failure."""
    report = sweep_view_roots(repo, project_id=pid)

    assert report == type(report)(examined=0, retired=0, purged_roots=0, purged_nodes=0)


def test_a_stale_candidate_hint_cannot_collect_a_root_that_got_published(repo, pid) -> None:
    """The candidate query takes no lock, so by the time a root is handled it may have been claimed.

    The hint is deliberately not the authorisation: `retire_root` re-decides under the root's write
    lock. This simulates the window by publishing a root after it would already have been listed as
    a candidate.
    """
    root_id = begin_root(
        repo, project_id=pid, view_key=VIEW, snapshot_id="snap-1", lease_seconds=0
    ).root_id
    _content(repo, pid)(root_id, lambda: None)

    # The root becomes publishable and is published -- exactly what a stale hint would not know.
    repo.execute(
        "MATCH (r:ViewRoot {root_id: $root}) SET r.lease_expires_at = timestamp() + 600000",
        {"root": root_id},
    )
    complete_root(repo, root_id=root_id)
    publish_root(
        repo, project_id=pid, view_key=VIEW, root_id=root_id, expected_generation=0
    )
    _expire(repo, root_id)

    report = sweep_view_roots(repo, project_id=pid)

    assert report.purged_roots == 0
    assert _count(repo, root_id) == 4
    assert read_view(repo, project_id=pid, view_key=VIEW).current_root == root_id


def test_sweeping_every_project_at_once_is_possible(repo, pid) -> None:
    """The scheduled shape: no project_id, so one pass covers the deployment."""
    other = f"p4-test-{uuid.uuid4().hex[:8]}"
    a = begin_root(repo, project_id=pid, view_key=VIEW, snapshot_id="s", lease_seconds=0).root_id
    b = begin_root(repo, project_id=other, view_key=VIEW, snapshot_id="s", lease_seconds=0).root_id

    sweep_view_roots(repo)

    assert read_root(repo, root_id=a).state == ROOT_ABANDONED
    assert read_root(repo, root_id=b).state == ROOT_ABANDONED
