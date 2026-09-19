"""P4 counterexamples for the promotion order: kill at every state, and both ways undo fails.

Design: `.agent/plans/menhir-snapshot-p4-graph-write-design-2026-09-17.md`.

The gate for this phase is written entirely as failure cases -- "deletion, failed write, process
kill at every state, and failed compensation have tested outcomes; the old project graph can be
restored from previous" -- so these are the gate, not a sample of it.

`write_structure` is a stub that can be made to fail at a chosen point. That is deliberate and is
worth more than driving a real writer: the property under test is what the ORDER guarantees when a
step dies, and a stub can die precisely where a real writer only dies occasionally.
"""

from __future__ import annotations

import os
import uuid

import pytest

from menhir.snapshot.canonical_view import (
    CANONICAL_VIEW_CONSTRAINTS,
    ERR_VIEW_DEGRADED,
    ViewError,
    mark_degraded,
    publish_root,
    read_view,
)
from menhir.snapshot.promotion import (
    ERR_PROMOTION_DEGRADED,
    ERR_PROMOTION_LEASE_LOST,
    ERR_PROMOTION_VERIFY_FAILED,
    ERR_PROMOTION_WRITE_FAILED,
    PromotionError,
    promote_snapshot,
)
from menhir.snapshot.view_root import (
    ROOT_BUILDING,
    ROOT_COMPLETE,
    VIEW_ROOT_CONSTRAINTS,
    VIEW_ROOT_PROPERTY,
    begin_root,
    complete_root,
    read_root,
    retire_root,
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
            "MATCH (r:ViewRoot) WHERE r.project_id STARTS WITH 'p4-test-' DETACH DELETE r", {}
        )
        r.execute("MATCH (n:SnapshotEntity) DETACH DELETE n", {})
        driver.close()


@pytest.fixture
def pid():
    return f"p4-test-{uuid.uuid4().hex[:8]}"


def _writer(calls: list | None = None, fail_with: BaseException | None = None):
    """A stub structure write. Records the root it was given; fails on demand."""

    def _write(root_id: str, renew_lease) -> None:
        if calls is not None:
            calls.append(root_id)
        if fail_with is not None:
            raise fail_with

    return _write


def _content(repo, count: int = 3):
    def _write(rid: str, renew_lease) -> None:
        repo.execute(
            f"UNWIND range(1, $n) AS i CREATE (:SnapshotEntity {{{VIEW_ROOT_PROPERTY}: $root}})",
            {"n": count, "root": rid},
        )

    return _write


def _roots(repo, pid):
    rows = repo.execute(
        "MATCH (r:ViewRoot {project_id: $pid}) RETURN r.root_id AS root_id, r.state AS state",
        {"pid": pid},
    )
    return {row["root_id"]: row["state"] for row in rows}


# --- the ordinary path -----------------------------------------------------------------------------


def test_a_promotion_publishes_the_root_it_built(repo, pid) -> None:
    calls: list = []

    outcome = promote_snapshot(
        repo,
        project_id=pid,
        view_key=VIEW,
        snapshot_id="snap-1",
        write_structure=_writer(calls),
    )

    assert calls == [outcome.root_id], "the writer must be told which root to tag"
    assert outcome.already_current is False
    assert outcome.reused_root is False
    view = read_view(repo, project_id=pid, view_key=VIEW)
    assert view.current_root == outcome.root_id
    assert view.generation == 1


def test_a_second_promotion_keeps_the_first_as_previous(repo, pid) -> None:
    """Restore-from-previous is only possible if promotion actually maintains it."""
    first = promote_snapshot(
        repo, project_id=pid, view_key=VIEW, snapshot_id="snap-1", write_structure=_writer()
    )
    second = promote_snapshot(
        repo, project_id=pid, view_key=VIEW, snapshot_id="snap-2", write_structure=_writer()
    )

    view = read_view(repo, project_id=pid, view_key=VIEW)
    assert view.current_root == second.root_id
    assert view.previous_root == first.root_id


# --- killed before the flip: nothing published, nothing to undo ------------------------------------


def test_a_failed_write_publishes_nothing_and_leaves_the_root_unreferenced(repo, pid) -> None:
    """The failed-write gate item.

    Nothing needs rolling back, and that is the whole reason the write happens off to the side. The
    root stays BUILDING, so it can never be published, and the sweeper reclaims it on lease expiry.
    """
    with pytest.raises(PromotionError) as excinfo:
        promote_snapshot(
            repo,
            project_id=pid,
            view_key=VIEW,
            snapshot_id="snap-1",
            write_structure=_writer(fail_with=RuntimeError("disk went away")),
        )

    assert excinfo.value.code == ERR_PROMOTION_WRITE_FAILED
    assert read_view(repo, project_id=pid, view_key=VIEW) is None
    assert set(_roots(repo, pid).values()) == {ROOT_BUILDING}


def test_an_interrupted_write_is_treated_the_same_as_a_failed_one(repo, pid) -> None:
    """KeyboardInterrupt is not an Exception, and the extraction writer learned that the hard way.

    A promotion killed mid-write must not leave a root that looks finished -- and cannot, because
    nothing has marked it COMPLETE.
    """
    with pytest.raises(PromotionError) as excinfo:
        promote_snapshot(
            repo,
            project_id=pid,
            view_key=VIEW,
            snapshot_id="snap-1",
            write_structure=_writer(fail_with=KeyboardInterrupt()),
        )

    assert excinfo.value.code == ERR_PROMOTION_WRITE_FAILED
    assert read_view(repo, project_id=pid, view_key=VIEW) is None
    assert set(_roots(repo, pid).values()) == {ROOT_BUILDING}


def test_verification_before_the_flip_costs_nothing_when_it_fails(repo, pid) -> None:
    """Which is exactly why a verifier that matters belongs here rather than after the flip."""

    def _reject(root_id: str) -> None:
        raise ValueError("fingerprint mismatch")

    with pytest.raises(PromotionError) as excinfo:
        promote_snapshot(
            repo,
            project_id=pid,
            view_key=VIEW,
            snapshot_id="snap-1",
            write_structure=_writer(),
            verify_before_flip=_reject,
        )

    assert excinfo.value.code == ERR_PROMOTION_WRITE_FAILED
    assert read_view(repo, project_id=pid, view_key=VIEW) is None


def test_a_root_abandoned_before_the_flip_can_never_be_published(repo, pid) -> None:
    """Kill after the write, before `complete_root`, then let the lease lapse.

    The sweeper's decision has to be final even against a builder that comes back.
    """
    building = begin_root(
        repo, project_id=pid, view_key=VIEW, snapshot_id="snap-1", lease_seconds=0
    )
    assert retire_root(repo, root_id=building.root_id) is True

    with pytest.raises(ViewError):
        publish_root(
            repo,
            project_id=pid,
            view_key=VIEW,
            root_id=building.root_id,
            expected_generation=0,
        )
    assert read_view(repo, project_id=pid, view_key=VIEW) is None


def test_a_write_that_outlives_its_lease_publishes_nothing(repo, pid) -> None:
    """The lease is real, and a writer that ignores `renew` loses its work rather than publishing it.

    Losing the work is the correct outcome: once the lease lapsed the sweeper was entitled to
    reclaim the root, so the builder can no longer prove what it wrote is still there.
    """
    with pytest.raises(PromotionError) as excinfo:
        promote_snapshot(
            repo,
            project_id=pid,
            view_key=VIEW,
            snapshot_id="snap-1",
            write_structure=_writer(),
            lease_seconds=0,
        )

    assert excinfo.value.code == ERR_PROMOTION_LEASE_LOST
    assert read_view(repo, project_id=pid, view_key=VIEW) is None


def test_renewing_during_a_long_write_keeps_the_promotion_alive(repo, pid) -> None:
    """The same write, renewing. This is what a batching writer must do between batches.

    The lease is expired from under the writer mid-write rather than started at zero: `renew` takes
    the promotion's own lease length, so renewing a zero-second lease renews it to zero and would
    prove nothing. A first version of this test did exactly that.
    """

    def _write(root_id: str, renew_lease) -> None:
        repo.execute(
            "MATCH (r:ViewRoot {root_id: $root}) SET r.lease_expires_at = 0", {"root": root_id}
        )
        assert read_root(repo, root_id=root_id).lease_live is False
        renew_lease()

    outcome = promote_snapshot(
        repo, project_id=pid, view_key=VIEW, snapshot_id="snap-1", write_structure=_write
    )

    assert read_view(repo, project_id=pid, view_key=VIEW).current_root == outcome.root_id


# --- killed after the flip: compensation ------------------------------------------------------------


def test_a_promotion_that_fails_verification_is_rolled_back_to_previous(repo, pid) -> None:
    """The restore-from-previous gate item, driven by the orchestration rather than by hand."""
    good = promote_snapshot(
        repo, project_id=pid, view_key=VIEW, snapshot_id="snap-good", write_structure=_writer()
    )

    def _reject(root_id: str) -> None:
        raise ValueError("the published view answers wrongly")

    with pytest.raises(PromotionError) as excinfo:
        promote_snapshot(
            repo,
            project_id=pid,
            view_key=VIEW,
            snapshot_id="snap-bad",
            write_structure=_writer(),
            verify_after_flip=_reject,
        )

    assert excinfo.value.code == ERR_PROMOTION_VERIFY_FAILED
    view = read_view(repo, project_id=pid, view_key=VIEW)
    assert view.current_root == good.root_id, "the old graph must be restored from previous"
    assert view.degraded is False, "a compensation that WORKED must not degrade the view"


def test_a_failed_compensation_degrades_the_view_and_says_so(repo, pid) -> None:
    """THE case most rollback designs assume away, and the one the gate names.

    A first promotion has no previous root, so there is nothing to roll back TO. The view is left
    serving a snapshot that failed verification -- and the only honest response is to make that
    loud: degraded, durable, promotions blocked, reads carrying the status.
    """

    def _reject(root_id: str) -> None:
        raise ValueError("the published view answers wrongly")

    with pytest.raises(PromotionError) as excinfo:
        promote_snapshot(
            repo,
            project_id=pid,
            view_key=VIEW,
            snapshot_id="snap-1",
            write_structure=_writer(),
            verify_after_flip=_reject,
        )

    assert excinfo.value.code == ERR_PROMOTION_DEGRADED
    assert excinfo.value.degraded is True
    view = read_view(repo, project_id=pid, view_key=VIEW)
    assert view.degraded is True
    assert view.current_root is not None, "the bad root is still live, which is why this is loud"


def test_a_degraded_view_refuses_the_next_promotion_entirely(repo, pid) -> None:
    """Fail-closed means nothing may be promoted INTO it until an operator acts."""
    promote_snapshot(
        repo, project_id=pid, view_key=VIEW, snapshot_id="snap-1", write_structure=_writer()
    )
    mark_degraded(repo, project_id=pid, view_key=VIEW, reason="compensation failed")

    with pytest.raises(ViewError) as excinfo:
        promote_snapshot(
            repo, project_id=pid, view_key=VIEW, snapshot_id="snap-2", write_structure=_writer()
        )

    assert excinfo.value.code == ERR_VIEW_DEGRADED


# --- retries, which are the normal case and not the exotic one -------------------------------------


def test_a_retry_after_a_successful_publish_does_not_republish(repo, pid) -> None:
    """The hazard that made `ERR_VIEW_ALREADY_CURRENT` necessary.

    An ambiguous failure -- the flip committed, the response never arrived -- means the caller
    retries. Without a guard the retry finds its own COMPLETE root and publishes it AGAIN, setting
    previous_root and current_root to the same root and silently destroying the one generation of
    undo the gate depends on.
    """
    first = promote_snapshot(
        repo, project_id=pid, view_key=VIEW, snapshot_id="snap-1", write_structure=_writer()
    )
    second = promote_snapshot(
        repo, project_id=pid, view_key=VIEW, snapshot_id="snap-2", write_structure=_writer()
    )
    before = read_view(repo, project_id=pid, view_key=VIEW)

    retry = promote_snapshot(
        repo, project_id=pid, view_key=VIEW, snapshot_id="snap-2", write_structure=_writer()
    )

    assert retry.already_current is True
    assert retry.root_id == second.root_id
    after = read_view(repo, project_id=pid, view_key=VIEW)
    assert after.current_root == second.root_id
    assert after.previous_root == first.root_id, "the retry must not overwrite previous"
    assert after.generation == before.generation, "a no-op retry must not advance the generation"


def test_a_retry_adopts_a_root_an_earlier_attempt_finished(repo, pid) -> None:
    """Rebuilding an identical root on every retry turns a flaky network into graph garbage."""
    built = complete_root(
        repo,
        root_id=begin_root(
            repo, project_id=pid, view_key=VIEW, snapshot_id="snap-1"
        ).root_id,
    )
    calls: list = []

    outcome = promote_snapshot(
        repo,
        project_id=pid,
        view_key=VIEW,
        snapshot_id="snap-1",
        write_structure=_writer(calls),
    )

    assert outcome.reused_root is True
    assert outcome.root_id == built.root_id
    assert calls == [], "an adopted root must not be written a second time"
    assert read_view(repo, project_id=pid, view_key=VIEW).current_root == built.root_id


def test_a_retry_does_not_adopt_a_root_that_is_still_being_written(repo, pid) -> None:
    """Another promotion may still hold it; adopting it would publish someone else's half."""
    in_flight = begin_root(repo, project_id=pid, view_key=VIEW, snapshot_id="snap-1")
    calls: list = []

    outcome = promote_snapshot(
        repo,
        project_id=pid,
        view_key=VIEW,
        snapshot_id="snap-1",
        write_structure=_writer(calls),
    )

    assert outcome.root_id != in_flight.root_id
    assert calls == [outcome.root_id]
    assert read_root(repo, root_id=in_flight.root_id).state == ROOT_BUILDING


# --- deletion, which is what the new-root strategy makes free --------------------------------------


def test_a_file_removed_from_the_project_leaves_the_graph_without_any_prune(repo, pid) -> None:
    """The deletion gate item, and the reason "no name-keyed prune remains" is satisfiable.

    Promotion writes a whole new root. A file absent from the second snapshot is absent from the
    second root because that root was never told about it -- not because anything matched it by
    name and deleted it. There is no prune to get wrong, which is #99's bug class having no door.
    """
    first = promote_snapshot(
        repo,
        project_id=pid,
        view_key=VIEW,
        snapshot_id="snap-1",
        write_structure=_content(repo, count=5),
    )
    second = promote_snapshot(
        repo,
        project_id=pid,
        view_key=VIEW,
        snapshot_id="snap-2",
        write_structure=_content(repo, count=3),
    )

    def _count(root_id: str) -> int:
        rows = repo.execute(
            f"MATCH (n:SnapshotEntity) WHERE n.{VIEW_ROOT_PROPERTY} = $root RETURN count(n) AS c",
            {"root": root_id},
        )
        return int(rows[0]["c"])

    assert _count(second.root_id) == 3, "the current root holds only what the snapshot declared"
    assert _count(first.root_id) == 5, "and the previous root is untouched, so undo still works"
    assert read_view(repo, project_id=pid, view_key=VIEW).previous_root == first.root_id


def test_the_root_left_behind_by_a_failed_promotion_is_reclaimable(repo, pid) -> None:
    """Garbage from a failed write must not accumulate forever."""
    with pytest.raises(PromotionError):
        promote_snapshot(
            repo,
            project_id=pid,
            view_key=VIEW,
            snapshot_id="snap-1",
            write_structure=_writer(fail_with=RuntimeError("boom")),
            lease_seconds=0,
        )

    orphan = next(iter(_roots(repo, pid)))
    assert retire_root(repo, root_id=orphan) is True


def test_a_completed_but_unpublished_root_is_not_mistaken_for_a_live_one(repo, pid) -> None:
    """Kill between `complete_root` and the flip: COMPLETE, but nothing references it."""
    built = complete_root(
        repo,
        root_id=begin_root(
            repo, project_id=pid, view_key=VIEW, snapshot_id="snap-1"
        ).root_id,
    )
    assert built.state == ROOT_COMPLETE
    # Completed while leased, then the process died before flipping and the lease lapsed.
    repo.execute(
        "MATCH (r:ViewRoot {root_id: $root}) SET r.lease_expires_at = 0", {"root": built.root_id}
    )

    assert read_view(repo, project_id=pid, view_key=VIEW) is None
    assert retire_root(repo, root_id=built.root_id) is True
