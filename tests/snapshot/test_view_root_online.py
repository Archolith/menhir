"""P4 counterexamples for the root lifecycle: what the pointer is allowed to point at.

Design: `.agent/plans/menhir-snapshot-p4-graph-write-design-2026-09-17.md`.

`test_canonical_view_online` proves the pointer cannot be moved by a loser. These prove the harder
half: that it cannot be moved ONTO something unfinished, foreign, or already being deleted -- and
that the sweeper reclaiming garbage can never reclaim what a reader still needs.

**Online by necessity.** The property under test is that a check and a write cannot be separated,
and what makes that true is Neo4j's lock, not our code. The probe-write in `retire_root` and
`publish_root` is meaningless against a fake, so a fake would report these as passing no matter
what the Cypher said.
"""

from __future__ import annotations

import os
import threading
import uuid

import pytest

from menhir.snapshot.canonical_view import (
    CANONICAL_VIEW_CONSTRAINTS,
    ERR_VIEW_ROOT_UNPUBLISHABLE,
    ViewError,
    publish_root,
    read_view,
    restore_previous,
)
from menhir.snapshot.view_root import (
    ERR_ROOT_NOT_BUILDING,
    ERR_ROOT_NOT_RETIRED,
    ERR_ROOT_UNKNOWN,
    ROOT_ABANDONED,
    ROOT_BUILDING,
    ROOT_COMPLETE,
    VIEW_ROOT_CONSTRAINTS,
    VIEW_ROOT_PROPERTY,
    RootError,
    begin_root,
    complete_root,
    find_publishable_root,
    purge_root,
    read_root,
    renew_root,
    retire_root,
)

pytestmark = [pytest.mark.online, pytest.mark.timeout(120)]

VIEW = "canonical"


class _Repo:
    def __init__(self, driver) -> None:
        #: Public on purpose: the two lock-ordering tests below need to hold a transaction open
        #: across another caller's statement, which a session-per-execute wrapper cannot express.
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
        r.execute("MATCH (n:P4TestContent) DETACH DELETE n", {})
        driver.close()


@pytest.fixture
def pid():
    """Prefixed so cleanup can never touch a view it did not create."""
    return f"p4-test-{uuid.uuid4().hex[:8]}"


def _complete_root(repo, pid, *, view_key=VIEW, snapshot_id="snap-1", lease_seconds=900):
    """A root built and finished the ordinary way. The tests never fabricate one by hand."""
    record = begin_root(
        repo,
        project_id=pid,
        view_key=view_key,
        snapshot_id=snapshot_id,
        lease_seconds=lease_seconds,
    )
    return complete_root(repo, root_id=record.root_id)


# --- a root under construction is not publishable --------------------------------------------------


def test_a_root_still_being_written_cannot_be_published(repo, pid) -> None:
    """THE counterexample for this module, and the one the first version of publish_root failed.

    This is the whole reason P4 writes to a new root instead of in place. If a BUILDING root could
    be published, the pointer would say "complete" about a write that is still happening, and a
    reader would get half a snapshot with nothing in the answer saying so -- the exact outcome the
    design names as more dangerous than a crash.
    """
    building = begin_root(repo, project_id=pid, view_key=VIEW, snapshot_id="snap-1")
    assert building.state == ROOT_BUILDING

    with pytest.raises(ViewError) as excinfo:
        publish_root(
            repo,
            project_id=pid,
            view_key=VIEW,
            root_id=building.root_id,
            expected_generation=0,
        )

    assert excinfo.value.code == ERR_VIEW_ROOT_UNPUBLISHABLE
    assert read_view(repo, project_id=pid, view_key=VIEW) is None


def test_a_root_from_another_project_cannot_be_published_into_this_view(repo, pid) -> None:
    """A generation CAS proves the view has not moved. It proves nothing about what goes into it.

    Publishing a foreign root would point one project's canonical view at another project's
    structure -- a cross-project read, which the parent plan lists as a condition for returning the
    whole feature to `off`.
    """
    other = f"p4-test-{uuid.uuid4().hex[:8]}"
    foreign = _complete_root(repo, other)

    with pytest.raises(ViewError) as excinfo:
        publish_root(
            repo, project_id=pid, view_key=VIEW, root_id=foreign.root_id, expected_generation=0
        )

    assert excinfo.value.code == ERR_VIEW_ROOT_UNPUBLISHABLE
    assert read_view(repo, project_id=pid, view_key=VIEW) is None


def test_a_root_built_for_another_view_of_the_same_project_cannot_be_published(repo, pid) -> None:
    """Same project is not the same view. P5 turns this into many views per project."""
    other_view = _complete_root(repo, pid, view_key="workspace-a")

    with pytest.raises(ViewError) as excinfo:
        publish_root(
            repo, project_id=pid, view_key=VIEW, root_id=other_view.root_id, expected_generation=0
        )

    assert excinfo.value.code == ERR_VIEW_ROOT_UNPUBLISHABLE


def test_a_root_that_does_not_exist_cannot_be_published(repo, pid) -> None:
    """A root id from a previous run, a typo, or a retry after a purge."""
    with pytest.raises(ViewError) as excinfo:
        publish_root(
            repo, project_id=pid, view_key=VIEW, root_id="vr-nonexistent", expected_generation=0
        )

    assert excinfo.value.code == ERR_VIEW_ROOT_UNPUBLISHABLE


# --- the lease, and the builder that lost it -------------------------------------------------------


def test_a_builder_whose_lease_expired_cannot_complete_its_root(repo, pid) -> None:
    """The sweeper's decision has to be final.

    Once the lease lapses the sweeper is entitled to retire this root and may already be deleting
    its nodes. "I finished" is no longer a claim this builder can make, so completion refuses --
    which is what stops a builder that was paged out for an hour from publishing a root whose
    content is half gone.
    """
    stale = begin_root(repo, project_id=pid, view_key=VIEW, snapshot_id="snap-1", lease_seconds=0)

    with pytest.raises(RootError) as excinfo:
        complete_root(repo, root_id=stale.root_id)

    assert excinfo.value.code == ERR_ROOT_NOT_BUILDING


def test_a_complete_root_whose_lease_expired_cannot_be_published(repo, pid) -> None:
    """Completion is not a permanent licence to publish.

    The gap between completing and publishing is where a builder can stall. An expired lease there
    means the sweeper may already have reclaimed the root, so the flip must refuse rather than
    point the view at something being deleted.
    """
    finished = _complete_root(repo, pid)
    assert finished.state == ROOT_COMPLETE
    # Completed while leased, then stalled long enough for the lease to lapse. Beginning with a
    # dead lease would not reach this state at all -- `complete_root` refuses that, which is its
    # own test above.
    repo.execute(
        "MATCH (r:ViewRoot {root_id: $root}) SET r.lease_expires_at = 0",
        {"root": finished.root_id},
    )

    with pytest.raises(ViewError) as excinfo:
        publish_root(
            repo, project_id=pid, view_key=VIEW, root_id=finished.root_id, expected_generation=0
        )

    assert excinfo.value.code == ERR_VIEW_ROOT_UNPUBLISHABLE


def test_renewing_keeps_a_long_build_publishable(repo, pid) -> None:
    """A lease checked once before a long operation is a lease that expires during it."""
    building = begin_root(repo, project_id=pid, view_key=VIEW, snapshot_id="snap-1", lease_seconds=0)
    assert read_root(repo, root_id=building.root_id).lease_live is False

    renewed = renew_root(repo, root_id=building.root_id, lease_seconds=900)

    assert renewed.lease_live is True
    finished = complete_root(repo, root_id=building.root_id)
    assert finished.state == ROOT_COMPLETE


def test_a_retired_root_cannot_be_renewed_back_to_life(repo, pid) -> None:
    """A builder that wakes up after the sweeper must not resurrect what it is deleting."""
    stale = begin_root(repo, project_id=pid, view_key=VIEW, snapshot_id="snap-1", lease_seconds=0)
    assert retire_root(repo, root_id=stale.root_id) is True

    with pytest.raises(RootError) as excinfo:
        renew_root(repo, root_id=stale.root_id)

    assert excinfo.value.code == ERR_ROOT_NOT_BUILDING
    assert read_root(repo, root_id=stale.root_id).state == ROOT_ABANDONED


# --- the sweeper must never reclaim what a reader needs --------------------------------------------


def test_the_sweeper_will_not_retire_a_root_a_builder_still_holds(repo, pid) -> None:
    """Lease expiry is the evidence, and it is the ONLY evidence.

    Not the process being gone, not elapsed wall time on the sweeper's own clock. The extraction
    lease learned this the hard way and the same rule binds here.
    """
    building = begin_root(repo, project_id=pid, view_key=VIEW, snapshot_id="snap-1")

    assert retire_root(repo, root_id=building.root_id) is False
    assert read_root(repo, root_id=building.root_id).state == ROOT_BUILDING


def test_the_sweeper_will_not_retire_the_current_root(repo, pid) -> None:
    """The current root's lease WILL expire -- it is published, not being built.

    So expiry alone can never authorise reclaiming it, and the reference check is what stands
    between a routine sweep and deleting the structure every reader is using.
    """
    root = _complete_root(repo, pid)
    publish_root(repo, project_id=pid, view_key=VIEW, root_id=root.root_id, expected_generation=0)
    repo.execute(
        "MATCH (r:ViewRoot {root_id: $root}) SET r.lease_expires_at = 0", {"root": root.root_id}
    )

    assert retire_root(repo, root_id=root.root_id) is False
    assert read_root(repo, root_id=root.root_id).state == ROOT_COMPLETE


def test_the_sweeper_will_not_retire_the_previous_root(repo, pid) -> None:
    """`previous` is the gate's escape hatch. A sweep that reclaims it removes the only undo."""
    first = _complete_root(repo, pid, snapshot_id="snap-1")
    second = _complete_root(repo, pid, snapshot_id="snap-2")
    publish_root(repo, project_id=pid, view_key=VIEW, root_id=first.root_id, expected_generation=0)
    publish_root(repo, project_id=pid, view_key=VIEW, root_id=second.root_id, expected_generation=1)
    repo.execute(
        "MATCH (r:ViewRoot {root_id: $root}) SET r.lease_expires_at = 0", {"root": first.root_id}
    )

    assert read_view(repo, project_id=pid, view_key=VIEW).previous_root == first.root_id
    assert retire_root(repo, root_id=first.root_id) is False


def test_a_root_demoted_past_previous_becomes_reclaimable(repo, pid) -> None:
    """Retention of exactly one, from the reclaim side.

    A third publish drops the first root out of both slots, and only THEN may it be reclaimed. This
    is the cost half of the retention decision: bounded at roughly two structures per project
    rather than growing with how often a project syncs.
    """
    roots = [_complete_root(repo, pid, snapshot_id=f"snap-{i}") for i in range(3)]
    for generation, root in enumerate(roots):
        publish_root(
            repo,
            project_id=pid,
            view_key=VIEW,
            root_id=root.root_id,
            expected_generation=generation,
        )
    repo.execute(
        "MATCH (r:ViewRoot {root_id: $root}) SET r.lease_expires_at = 0", {"root": roots[0].root_id}
    )

    assert retire_root(repo, root_id=roots[0].root_id) is True
    assert read_root(repo, root_id=roots[0].root_id).state == ROOT_ABANDONED


def test_a_root_unreferenced_after_a_restore_becomes_reclaimable(repo, pid) -> None:
    """Restoring drops `previous`, which is the other way a root stops being referenced."""
    good = _complete_root(repo, pid, snapshot_id="snap-good")
    bad = _complete_root(repo, pid, snapshot_id="snap-bad")
    publish_root(repo, project_id=pid, view_key=VIEW, root_id=good.root_id, expected_generation=0)
    published = publish_root(
        repo, project_id=pid, view_key=VIEW, root_id=bad.root_id, expected_generation=1
    )
    restore_previous(
        repo, project_id=pid, view_key=VIEW, expected_generation=published.generation
    )
    repo.execute(
        "MATCH (r:ViewRoot {root_id: $root}) SET r.lease_expires_at = 0", {"root": bad.root_id}
    )

    assert read_view(repo, project_id=pid, view_key=VIEW).current_root == good.root_id
    assert retire_root(repo, root_id=bad.root_id) is True


def test_retiring_is_idempotent(repo, pid) -> None:
    """Two sweepers, or one sweeper retried. The second call is a no-op, not a second retirement."""
    stale = begin_root(repo, project_id=pid, view_key=VIEW, snapshot_id="snap-1", lease_seconds=0)

    assert retire_root(repo, root_id=stale.root_id) is True
    assert retire_root(repo, root_id=stale.root_id) is False


# --- the probe write, which is the only thing making the guards above concurrency-safe -------------
#
# Every test up to here runs its two operations one after the other, so they would ALL still pass
# with the probe removed and the TOCTOU wide open. These two are the ones that fail without it.


def _run_detached(fn):
    """Run `fn` on a thread and return a handle that records its outcome."""
    outcome: dict = {}

    def _target() -> None:
        try:
            outcome["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - recorded and re-examined by the caller
            outcome["error"] = exc

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    return thread, outcome


def test_a_publish_cannot_read_the_root_while_a_sweeper_holds_its_lock(repo, pid) -> None:
    """The negative control for `publish_root`'s probe write, and it needs real concurrency.

    Without `SET r.publish_probe`, the publish would MATCH the root with a plain read -- which
    Neo4j's read-committed isolation happily serves from the last commit, not blocked by the
    sweeper's open transaction. The publish would decide the root was COMPLETE and leased, and the
    sweeper would then commit its retirement on top. The view ends up pointing at a root queued for
    deletion, and every read either side made was true when it was made.

    So: hold the sweeper's write lock, and assert the publish BLOCKS rather than deciding.
    """
    root = _complete_root(repo, pid)

    with repo.driver.session() as sweeper:
        tx = sweeper.begin_transaction()
        tx.run(
            "MATCH (r:ViewRoot {root_id: $root}) SET r.retire_probe = timestamp()",
            root=root.root_id,
        )

        thread, outcome = _run_detached(
            lambda: publish_root(
                repo,
                project_id=pid,
                view_key=VIEW,
                root_id=root.root_id,
                expected_generation=0,
            )
        )
        thread.join(timeout=2.0)

        assert thread.is_alive(), (
            "the publish reached a decision while the sweeper held the root's write lock; "
            "its authorising read is not under that lock"
        )
        tx.rollback()

    thread.join(timeout=10.0)
    assert not thread.is_alive()
    assert "error" not in outcome, outcome.get("error")
    assert outcome["value"].current_root == root.root_id


def test_a_sweeper_cannot_read_the_view_while_a_publish_holds_the_root_lock(repo, pid) -> None:
    """The same control for `retire_root`, in the direction that actually loses data.

    This is the interleaving that deletes a live view's structure: the sweeper reads a view that
    does not yet reference the root, a publish commits, and the sweeper retires the root the view
    has just made current. Locking the root before reading the view is what forecloses it -- and
    both paths take the root first, so they serialise instead of deadlocking.

    **Asserting that the sweeper BLOCKS is not enough, and a negative control caught that.** With
    the probe removed the sweeper still blocks -- just at its final write, after it has already
    read the stale view -- so a blocking assertion passes either way. What separates the two is the
    OUTCOME: the sweeper must come back having seen the publish that committed while it waited.
    """
    # The view must ALREADY EXIST, and a first attempt at this test got that wrong. When the
    # in-flight publish creates the view node itself, the uniqueness constraint's index lock
    # serialises the two on its own and the test passes with the probe removed -- it proves
    # Neo4j's constraint works, not our ordering. A second publish onto an existing view MERGEs a
    # node that is already there, which takes no lock, and that is the real window.
    established = _complete_root(repo, pid, snapshot_id="snap-established")
    publish_root(
        repo, project_id=pid, view_key=VIEW, root_id=established.root_id, expected_generation=0
    )

    root = _complete_root(repo, pid, snapshot_id="snap-incoming")
    # The lease must be expired or the sweeper short-circuits on its own guard and never reaches
    # the lock at all. That is the real window: a publish whose authorising read happened while the
    # lease was still live, committing after it lapsed.
    repo.execute(
        "MATCH (r:ViewRoot {root_id: $root}) SET r.lease_expires_at = 0", {"root": root.root_id}
    )

    with repo.driver.session() as publisher:
        tx = publisher.begin_transaction()
        # A publish in flight: it holds the root's lock and has moved the pointer, but has not
        # committed, so the view a concurrent reader sees still does not reference this root.
        tx.run(
            "MATCH (r:ViewRoot {root_id: $root}) SET r.publish_probe = timestamp()",
            root=root.root_id,
        )
        tx.run(
            "MATCH (v:CanonicalView {project_id: $pid, view_key: $vk}) "
            "SET v.previous_root = v.current_root, v.current_root = $root, v.generation = 2",
            pid=pid,
            vk=VIEW,
            root=root.root_id,
        )

        thread, outcome = _run_detached(lambda: retire_root(repo, root_id=root.root_id))
        thread.join(timeout=2.0)

        assert thread.is_alive(), "the sweeper did not wait for the root's write lock at all"
        tx.commit()

    thread.join(timeout=10.0)
    assert not thread.is_alive()
    assert "error" not in outcome, outcome.get("error")
    assert outcome["value"] is False, (
        "the sweeper retired a root that the publish it waited for had just made current; "
        "it read the view before taking the root's lock"
    )
    assert read_root(repo, root_id=root.root_id).state == ROOT_COMPLETE


# --- content deletion is gated on the retirement being durable first -------------------------------


def _write_content(repo, root_id: str, count: int) -> None:
    repo.execute(
        f"UNWIND range(1, $n) AS i CREATE (:P4TestContent {{{VIEW_ROOT_PROPERTY}: $root, i: i}})",
        {"n": count, "root": root_id},
    )


def _content_count(repo, root_id: str) -> int:
    rows = repo.execute(
        f"MATCH (n:P4TestContent) WHERE n.{VIEW_ROOT_PROPERTY} = $root RETURN count(n) AS c",
        {"root": root_id},
    )
    return int(rows[0]["c"])


def test_content_cannot_be_purged_until_the_root_is_retired(repo, pid) -> None:
    """The ordering invariant, and the one that makes every other guarantee here real.

    `retire_root` is where "this root can never be published again" becomes durable. Purging before
    that would destroy content while the root was still a legal publish target -- so a purge that
    re-derived the decision itself, rather than requiring the recorded one, could race the flip.
    """
    building = begin_root(repo, project_id=pid, view_key=VIEW, snapshot_id="snap-1")
    _write_content(repo, building.root_id, 5)

    with pytest.raises(RootError) as excinfo:
        purge_root(repo, root_id=building.root_id)

    assert excinfo.value.code == ERR_ROOT_NOT_RETIRED
    assert _content_count(repo, building.root_id) == 5


def test_purging_removes_only_the_retired_roots_content(repo, pid) -> None:
    """A purge that reached past its own root would be #99 with a new key."""
    doomed = begin_root(repo, project_id=pid, view_key=VIEW, snapshot_id="snap-1", lease_seconds=0)
    live = _complete_root(repo, pid, snapshot_id="snap-2")
    _write_content(repo, doomed.root_id, 7)
    _write_content(repo, live.root_id, 4)
    retire_root(repo, root_id=doomed.root_id)

    removed = purge_root(repo, root_id=doomed.root_id, batch=3)

    assert removed == 7
    assert _content_count(repo, doomed.root_id) == 0
    assert _content_count(repo, live.root_id) == 4, "a live root's content must be untouched"


def test_a_purge_that_died_halfway_is_finished_by_the_next_sweep(repo, pid) -> None:
    """Partially deleted is a SAFE resting state here, which is unusual and worth pinning.

    It is safe only because the root is already retired: nothing may read it and nothing may
    publish it, so a half-deleted root is garbage either way. That is the compensating design for
    "a half-written graph cannot be removed in one call".
    """
    doomed = begin_root(repo, project_id=pid, view_key=VIEW, snapshot_id="snap-1", lease_seconds=0)
    _write_content(repo, doomed.root_id, 10)
    retire_root(repo, root_id=doomed.root_id)

    # A purge that managed one batch and then died.
    repo.execute(
        f"MATCH (n:P4TestContent) WHERE n.{VIEW_ROOT_PROPERTY} = $root "
        "WITH n LIMIT 4 DETACH DELETE n",
        {"root": doomed.root_id},
    )
    assert _content_count(repo, doomed.root_id) == 6

    assert purge_root(repo, root_id=doomed.root_id) == 6
    assert _content_count(repo, doomed.root_id) == 0


def test_purging_an_unknown_root_refuses_rather_than_deleting_nothing_quietly(repo, pid) -> None:
    """A silent no-op would let a typo look like a successful reclaim forever."""
    with pytest.raises(RootError) as excinfo:
        purge_root(repo, root_id="vr-nonexistent")

    assert excinfo.value.code == ERR_ROOT_UNKNOWN


# --- promotion retried after an ambiguous failure --------------------------------------------------


def test_a_retried_promotion_finds_the_root_it_already_built(repo, pid) -> None:
    """A retry after an ambiguous failure is the normal case, not the exotic one.

    Rebuilding an identical root on every retry would turn a flaky network into a steady source of
    graph garbage, each copy needing its own sweep.
    """
    built = _complete_root(repo, pid, snapshot_id="snap-abc")

    found = find_publishable_root(repo, project_id=pid, view_key=VIEW, snapshot_id="snap-abc")

    assert found is not None
    assert found.root_id == built.root_id


def test_an_unfinished_root_is_not_offered_to_a_retry(repo, pid) -> None:
    """Another promotion may still be writing it; adopting it would publish someone else's half."""
    begin_root(repo, project_id=pid, view_key=VIEW, snapshot_id="snap-abc")

    assert find_publishable_root(repo, project_id=pid, view_key=VIEW, snapshot_id="snap-abc") is None


def test_a_root_for_a_different_snapshot_is_not_offered_to_a_retry(repo, pid) -> None:
    """Idempotency is keyed on the snapshot, so a different upload never reuses this work."""
    _complete_root(repo, pid, snapshot_id="snap-abc")

    assert find_publishable_root(repo, project_id=pid, view_key=VIEW, snapshot_id="snap-xyz") is None


def test_root_ids_are_never_reused(repo, pid) -> None:
    """Two builds, two ids, always.

    The extraction lease shipped with a reusable generation and it was a real bug: a second build
    inheriting the first one's nodes would publish a blend of two snapshots that never existed.
    """
    first = begin_root(repo, project_id=pid, view_key=VIEW, snapshot_id="snap-1")
    second = begin_root(repo, project_id=pid, view_key=VIEW, snapshot_id="snap-1")

    assert first.root_id != second.root_id
