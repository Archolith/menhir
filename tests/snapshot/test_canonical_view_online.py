"""P4 counterexamples: the failures the view pointer must survive, against a real Neo4j.

Design: `.agent/plans/menhir-snapshot-p4-graph-write-design-2026-09-17.md`.

Written before the promotion path exists, because the gate is written as failure cases: kill at
every state, two promotions racing, compensation failing, restore from previous. Every bug found
in this project today came from building the failure first.

**Online by necessity, not preference.** The property under test is that a check and a write cannot
be separated -- and the thing that makes that true is Neo4j's lock, not our code. A fake would
pass whatever semantics I gave it, which is exactly the test that proves nothing.
`test_cf257_identity_binding_online.py` makes the same argument for the identity fence, and this
follows its shape: real driver, the SHIPPED DDL rather than a copy, prefixed ids, scoped cleanup.

**These build real roots rather than naming string ids.** They used to pass `"root-1"`, which
passed only because `publish_root` validated nothing about what it published. Minting the root the
ordinary way is what keeps these tests about the pointer's CAS instead of quietly re-admitting the
hole `test_view_root_online` now covers.
"""

from __future__ import annotations

import os
import uuid

import pytest

from menhir.snapshot.canonical_view import (
    CANONICAL_VIEW_CONSTRAINTS,
    ERR_VIEW_ACTOR_REQUIRED,
    ERR_VIEW_DEGRADED,
    ERR_VIEW_NO_PREVIOUS,
    ERR_VIEW_SUPERSEDED,
    ViewError,
    mark_degraded,
    publish_root,
    read_view,
    restore_previous,
)
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
    r = _Repo(driver)
    # The shipped DDL, not a copy: a test that writes its own constraint proves nothing about the
    # one production applies.
    for statement in [*CANONICAL_VIEW_CONSTRAINTS, *VIEW_ROOT_CONSTRAINTS]:
        r.execute(statement, {})
    try:
        yield r
    finally:
        r.execute(
            "MATCH (v:CanonicalView) WHERE v.project_id STARTS WITH 'p4-test-' DETACH DELETE v",
            {},
        )
        r.execute(
            "MATCH (r:ViewRoot) WHERE r.project_id STARTS WITH 'p4-test-' DETACH DELETE r",
            {},
        )
        driver.close()


@pytest.fixture
def pid():
    """Prefixed so cleanup can never touch a view it did not create."""
    return f"p4-test-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def mint(repo, pid):
    """Build and finish a root the ordinary way, returning its id.

    Deliberately goes through `begin_root` + `complete_root` rather than writing a node directly:
    a fixture that fabricates a COMPLETE root by hand would keep passing if the lifecycle that
    produces one broke.
    """

    def _mint(snapshot_id: str) -> str:
        record = begin_root(
            repo, project_id=pid, view_key=VIEW, snapshot_id=snapshot_id
        )
        return complete_root(repo, root_id=record.root_id).root_id

    return _mint


# --- the pointer is what makes a build visible -----------------------------------------------------


def test_a_project_with_no_promotion_has_no_view(repo, pid) -> None:
    """Absence is not an empty view; nothing has been published."""
    assert read_view(repo, project_id=pid, view_key=VIEW) is None


@pytest.mark.parametrize("actor", ["", "   "])
def test_publish_refuses_empty_actor(repo, pid, mint, actor) -> None:
    root = mint("snap-empty-actor")

    with pytest.raises(ViewError) as excinfo:
        publish_root(
            repo,
            project_id=pid,
            view_key=VIEW,
            root_id=root,
            expected_generation=0,
            actor=actor,
        )

    assert excinfo.value.code == ERR_VIEW_ACTOR_REQUIRED
    view = read_view(repo, project_id=pid, view_key=VIEW)
    assert view.current_root is None
    assert view.last_promoted_by == ""


def test_restore_refuses_empty_actor(repo, pid, mint) -> None:
    first, second = mint("snap-first"), mint("snap-second")
    publish_root(
        repo, project_id=pid, view_key=VIEW, root_id=first, expected_generation=0,
        actor="test:operator",
    )
    published = publish_root(
        repo, project_id=pid, view_key=VIEW, root_id=second, expected_generation=1,
        actor="test:operator",
    )

    with pytest.raises(ViewError) as excinfo:
        restore_previous(
            repo,
            project_id=pid,
            view_key=VIEW,
            expected_generation=published.generation,
            actor="",
        )

    assert excinfo.value.code == ERR_VIEW_ACTOR_REQUIRED
    assert read_view(repo, project_id=pid, view_key=VIEW).current_root == second


def test_mark_degraded_refuses_empty_actor(repo, pid, mint) -> None:
    root = mint("snap-before-degraded")
    publish_root(
        repo, project_id=pid, view_key=VIEW, root_id=root, expected_generation=0,
        actor="test:operator",
    )

    with pytest.raises(ViewError) as excinfo:
        mark_degraded(
            repo,
            project_id=pid,
            view_key=VIEW,
            reason="must not land",
            actor=" ",
        )

    assert excinfo.value.code == ERR_VIEW_ACTOR_REQUIRED
    assert read_view(repo, project_id=pid, view_key=VIEW).degraded is False


def test_successful_publish_records_last_promoted_by(repo, pid, mint) -> None:
    root = mint("snap-attributed")

    published = publish_root(
        repo, project_id=pid, view_key=VIEW, root_id=root, expected_generation=0,
        actor="test:operator",
    )

    assert published.last_promoted_by == "test:operator"
    assert read_view(repo, project_id=pid, view_key=VIEW).last_promoted_by == "test:operator"


def test_publishing_moves_current_and_keeps_exactly_one_previous(repo, pid, mint) -> None:
    """The retention decision, asserted rather than described.

    Two publishes leave `previous` pointing at the FIRST root. A third would drop it -- one
    generation of undo, bounded cost, and a bad promotion discovered after a later good one is not
    reversible in place. That trade was made deliberately; this is where it is visible.
    """
    r1, r2, r3 = mint("snap-1"), mint("snap-2"), mint("snap-3")

    first = publish_root(
        repo, project_id=pid, view_key=VIEW, root_id=r1, expected_generation=0,
        actor="test:operator",
    )
    assert first.current_root == r1
    assert first.previous_root is None

    second = publish_root(
        repo, project_id=pid, view_key=VIEW, root_id=r2, expected_generation=1,
        actor="test:operator",
    )
    assert second.current_root == r2
    assert second.previous_root == r1

    third = publish_root(
        repo, project_id=pid, view_key=VIEW, root_id=r3, expected_generation=2,
        actor="test:operator",
    )
    assert third.current_root == r3
    assert third.previous_root == r2, "retention is one generation, not a history"


# --- two promotions racing -------------------------------------------------------------------------


def test_a_promotion_built_against_a_stale_generation_is_refused(repo, pid, mint) -> None:
    """THE counterexample for this module.

    A build takes minutes. If another promotion publishes during it, the generation this one was
    authorised under is no longer the truth -- and publishing anyway would silently discard a
    promotion that legitimately happened.

    This is the identity fence's failure in a different costume: a decision made from a fact read
    earlier, applied after the fact changed.
    """
    r1, r2, slow = mint("snap-1"), mint("snap-2"), mint("snap-slow")
    publish_root(
        repo, project_id=pid, view_key=VIEW, root_id=r1, expected_generation=0,
        actor="test:operator",
    )
    publish_root(
        repo, project_id=pid, view_key=VIEW, root_id=r2, expected_generation=1,
        actor="test:operator",
    )

    # A promotion that started before r2 landed still believes the generation is 1.
    with pytest.raises(ViewError) as excinfo:
        publish_root(
            repo, project_id=pid, view_key=VIEW, root_id=slow, expected_generation=1,
            actor="test:operator",
        )

    assert excinfo.value.code == ERR_VIEW_SUPERSEDED
    assert read_view(repo, project_id=pid, view_key=VIEW).current_root == r2


def test_the_loser_of_a_race_does_not_overwrite_the_winner(repo, pid, mint) -> None:
    """Both promotions read generation 0 and both try to publish; exactly one may win."""
    winner, loser = mint("snap-winner"), mint("snap-loser")
    publish_root(
        repo, project_id=pid, view_key=VIEW, root_id=winner, expected_generation=0,
        actor="test:operator",
    )

    with pytest.raises(ViewError) as excinfo:
        publish_root(
            repo, project_id=pid, view_key=VIEW, root_id=loser, expected_generation=0,
            actor="test:operator",
        )

    assert excinfo.value.code == ERR_VIEW_SUPERSEDED
    view = read_view(repo, project_id=pid, view_key=VIEW)
    assert view.current_root == winner
    assert view.generation == 1, "a refused publish must not advance the generation"


# --- compensation ----------------------------------------------------------------------------------


def test_restoring_previous_undoes_the_last_promotion(repo, pid, mint) -> None:
    """The escape hatch the gate requires, exercised rather than assumed."""
    good, bad = mint("snap-good"), mint("snap-bad")
    publish_root(
        repo, project_id=pid, view_key=VIEW, root_id=good, expected_generation=0,
        actor="test:operator",
    )
    published = publish_root(
        repo, project_id=pid, view_key=VIEW, root_id=bad, expected_generation=1,
        actor="test:operator",
    )

    restored = restore_previous(
        repo, project_id=pid, view_key=VIEW, expected_generation=published.generation,
        actor="test:operator",
    )

    assert restored.current_root == good


def test_restoring_twice_refuses_rather_than_guessing(repo, pid, mint) -> None:
    """The consequence of one-generation retention, and the reason it must be loud.

    After one restore there is no previous root left. Restoring again could only pick something
    the operator did not ask for, so it refuses. An escape hatch that opens onto the wrong room is
    worse than one that says no.
    """
    good, bad = mint("snap-good"), mint("snap-bad")
    publish_root(
        repo, project_id=pid, view_key=VIEW, root_id=good, expected_generation=0,
        actor="test:operator",
    )
    published = publish_root(
        repo, project_id=pid, view_key=VIEW, root_id=bad, expected_generation=1,
        actor="test:operator",
    )
    restored = restore_previous(
        repo, project_id=pid, view_key=VIEW, expected_generation=published.generation,
        actor="test:operator",
    )

    with pytest.raises(ViewError) as excinfo:
        restore_previous(
            repo, project_id=pid, view_key=VIEW, expected_generation=restored.generation,
            actor="test:operator",
        )

    assert excinfo.value.code == ERR_VIEW_NO_PREVIOUS
    assert read_view(repo, project_id=pid, view_key=VIEW).current_root == good


def test_a_project_promoted_only_once_cannot_be_restored(repo, pid, mint) -> None:
    """There is nothing to go back to, and saying so beats inventing one."""
    only = mint("snap-only")
    published = publish_root(
        repo, project_id=pid, view_key=VIEW, root_id=only, expected_generation=0,
        actor="test:operator",
    )

    with pytest.raises(ViewError) as excinfo:
        restore_previous(
            repo,
            project_id=pid,
            view_key=VIEW,
            expected_generation=published.generation,
            actor="test:operator",
        )

    assert excinfo.value.code == ERR_VIEW_NO_PREVIOUS


# --- degraded --------------------------------------------------------------------------------------


def test_a_degraded_view_refuses_promotion(repo, pid, mint) -> None:
    """Failed compensation leaves a state no code path intended; nothing may be promoted into it."""
    r1, r2 = mint("snap-1"), mint("snap-2")
    publish_root(
        repo, project_id=pid, view_key=VIEW, root_id=r1, expected_generation=0,
        actor="test:operator",
    )
    mark_degraded(
        repo, project_id=pid, view_key=VIEW, reason="compensation failed",
        actor="test:operator",
    )

    with pytest.raises(ViewError) as excinfo:
        publish_root(
            repo, project_id=pid, view_key=VIEW, root_id=r2, expected_generation=1,
            actor="test:operator",
        )

    assert excinfo.value.code == ERR_VIEW_DEGRADED


def test_a_degraded_view_still_serves_reads_carrying_its_status(repo, pid, mint) -> None:
    """The plan is explicit and this pins it (parent plan line 738).

    Fail-closed means nothing may be promoted INTO a degraded view. It does NOT mean reads are
    refused: blocking those would take a project's memory away over a fault in the write path,
    and the caller can see the status and decide.
    """
    r1 = mint("snap-1")
    publish_root(
        repo, project_id=pid, view_key=VIEW, root_id=r1, expected_generation=0,
        actor="test:operator",
    )
    mark_degraded(
        repo, project_id=pid, view_key=VIEW, reason="compensation failed",
        actor="test:operator",
    )

    view = read_view(repo, project_id=pid, view_key=VIEW)

    assert view is not None, "a degraded view must still be readable"
    assert view.current_root == r1
    assert view.degraded is True
    assert view.degraded_reason == "compensation failed"


def test_marking_degraded_survives_a_generation_change(repo, pid, mint) -> None:
    """Deliberately unconditional.

    A view is marked degraded when compensation has ALREADY failed. Refusing to record that
    because the generation moved would leave the fault invisible, which is the opposite of what
    the mark is for.
    """
    r1, r2 = mint("snap-1"), mint("snap-2")
    publish_root(
        repo, project_id=pid, view_key=VIEW, root_id=r1, expected_generation=0,
        actor="test:operator",
    )
    publish_root(
        repo, project_id=pid, view_key=VIEW, root_id=r2, expected_generation=1,
        actor="test:operator",
    )

    marked = mark_degraded(
        repo, project_id=pid, view_key=VIEW, reason="late", actor="test:operator"
    )

    assert marked.degraded is True
