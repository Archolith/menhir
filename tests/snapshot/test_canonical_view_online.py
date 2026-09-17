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
"""

from __future__ import annotations

import os
import uuid

import pytest

from menhir.snapshot.canonical_view import (
    CANONICAL_VIEW_CONSTRAINTS,
    ERR_VIEW_DEGRADED,
    ERR_VIEW_NO_PREVIOUS,
    ERR_VIEW_SUPERSEDED,
    ViewError,
    mark_degraded,
    publish_root,
    read_view,
    restore_previous,
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
    for statement in CANONICAL_VIEW_CONSTRAINTS:
        r.execute(statement, {})
    try:
        yield r
    finally:
        r.execute(
            "MATCH (v:CanonicalView) WHERE v.project_id STARTS WITH 'p4-test-' DETACH DELETE v",
            {},
        )
        driver.close()


@pytest.fixture
def pid():
    """Prefixed so cleanup can never touch a view it did not create."""
    return f"p4-test-{uuid.uuid4().hex[:8]}"


# --- the pointer is what makes a build visible -----------------------------------------------------


def test_a_project_with_no_promotion_has_no_view(repo, pid) -> None:
    """Absence is not an empty view; nothing has been published."""
    assert read_view(repo, project_id=pid, view_key=VIEW) is None


def test_publishing_moves_current_and_keeps_exactly_one_previous(repo, pid) -> None:
    """The retention decision, asserted rather than described.

    Two publishes leave `previous` pointing at the FIRST root. A third would drop it -- one
    generation of undo, bounded cost, and a bad promotion discovered after a later good one is not
    reversible in place. That trade was made deliberately; this is where it is visible.
    """
    first = publish_root(
        repo, project_id=pid, view_key=VIEW, root_id="root-1", expected_generation=0
    )
    assert first.current_root == "root-1"
    assert first.previous_root is None

    second = publish_root(
        repo, project_id=pid, view_key=VIEW, root_id="root-2", expected_generation=1
    )
    assert second.current_root == "root-2"
    assert second.previous_root == "root-1"

    third = publish_root(
        repo, project_id=pid, view_key=VIEW, root_id="root-3", expected_generation=2
    )
    assert third.current_root == "root-3"
    assert third.previous_root == "root-2", "retention is one generation, not a history"


# --- two promotions racing -------------------------------------------------------------------------


def test_a_promotion_built_against_a_stale_generation_is_refused(repo, pid) -> None:
    """THE counterexample for this module.

    A build takes minutes. If another promotion publishes during it, the generation this one was
    authorised under is no longer the truth -- and publishing anyway would silently discard a
    promotion that legitimately happened.

    This is the identity fence's failure in a different costume: a decision made from a fact read
    earlier, applied after the fact changed.
    """
    publish_root(
        repo, project_id=pid, view_key=VIEW, root_id="root-1", expected_generation=0
    )
    publish_root(
        repo, project_id=pid, view_key=VIEW, root_id="root-2", expected_generation=1
    )

    # A promotion that started before root-2 landed still believes the generation is 1.
    with pytest.raises(ViewError) as excinfo:
        publish_root(
            repo,
            project_id=pid,
            view_key=VIEW,
            root_id="root-slow",
            expected_generation=1,
        )

    assert excinfo.value.code == ERR_VIEW_SUPERSEDED
    assert read_view(repo, project_id=pid, view_key=VIEW).current_root == "root-2"


def test_the_loser_of_a_race_does_not_overwrite_the_winner(repo, pid) -> None:
    """Both promotions read generation 0 and both try to publish; exactly one may win."""
    publish_root(
        repo, project_id=pid, view_key=VIEW, root_id="winner", expected_generation=0
    )

    with pytest.raises(ViewError) as excinfo:
        publish_root(
            repo, project_id=pid, view_key=VIEW, root_id="loser", expected_generation=0
        )

    assert excinfo.value.code == ERR_VIEW_SUPERSEDED
    view = read_view(repo, project_id=pid, view_key=VIEW)
    assert view.current_root == "winner"
    assert view.generation == 1, "a refused publish must not advance the generation"


# --- compensation ----------------------------------------------------------------------------------


def test_restoring_previous_undoes_the_last_promotion(repo, pid) -> None:
    """The escape hatch the gate requires, exercised rather than assumed."""
    publish_root(
        repo, project_id=pid, view_key=VIEW, root_id="good", expected_generation=0
    )
    published = publish_root(
        repo, project_id=pid, view_key=VIEW, root_id="bad", expected_generation=1
    )

    restored = restore_previous(
        repo, project_id=pid, view_key=VIEW, expected_generation=published.generation
    )

    assert restored.current_root == "good"


def test_restoring_twice_refuses_rather_than_guessing(repo, pid) -> None:
    """The consequence of one-generation retention, and the reason it must be loud.

    After one restore there is no previous root left. Restoring again could only pick something
    the operator did not ask for, so it refuses. An escape hatch that opens onto the wrong room is
    worse than one that says no.
    """
    publish_root(
        repo, project_id=pid, view_key=VIEW, root_id="good", expected_generation=0
    )
    published = publish_root(
        repo, project_id=pid, view_key=VIEW, root_id="bad", expected_generation=1
    )
    restored = restore_previous(
        repo, project_id=pid, view_key=VIEW, expected_generation=published.generation
    )

    with pytest.raises(ViewError) as excinfo:
        restore_previous(
            repo, project_id=pid, view_key=VIEW, expected_generation=restored.generation
        )

    assert excinfo.value.code == ERR_VIEW_NO_PREVIOUS
    assert read_view(repo, project_id=pid, view_key=VIEW).current_root == "good"


def test_a_project_promoted_only_once_cannot_be_restored(repo, pid) -> None:
    """There is nothing to go back to, and saying so beats inventing one."""
    published = publish_root(
        repo, project_id=pid, view_key=VIEW, root_id="only", expected_generation=0
    )

    with pytest.raises(ViewError) as excinfo:
        restore_previous(
            repo,
            project_id=pid,
            view_key=VIEW,
            expected_generation=published.generation,
        )

    assert excinfo.value.code == ERR_VIEW_NO_PREVIOUS


# --- degraded --------------------------------------------------------------------------------------


def test_a_degraded_view_refuses_promotion(repo, pid) -> None:
    """Failed compensation leaves a state no code path intended; nothing may be promoted into it."""
    publish_root(
        repo, project_id=pid, view_key=VIEW, root_id="root-1", expected_generation=0
    )
    mark_degraded(repo, project_id=pid, view_key=VIEW, reason="compensation failed")

    with pytest.raises(ViewError) as excinfo:
        publish_root(
            repo, project_id=pid, view_key=VIEW, root_id="root-2", expected_generation=1
        )

    assert excinfo.value.code == ERR_VIEW_DEGRADED


def test_a_degraded_view_still_serves_reads_carrying_its_status(repo, pid) -> None:
    """The plan is explicit and this pins it (parent plan line 738).

    Fail-closed means nothing may be promoted INTO a degraded view. It does NOT mean reads are
    refused: blocking those would take a project's memory away over a fault in the write path,
    and the caller can see the status and decide.
    """
    publish_root(
        repo, project_id=pid, view_key=VIEW, root_id="root-1", expected_generation=0
    )
    mark_degraded(repo, project_id=pid, view_key=VIEW, reason="compensation failed")

    view = read_view(repo, project_id=pid, view_key=VIEW)

    assert view is not None, "a degraded view must still be readable"
    assert view.current_root == "root-1"
    assert view.degraded is True
    assert view.degraded_reason == "compensation failed"


def test_marking_degraded_survives_a_generation_change(repo, pid) -> None:
    """Deliberately unconditional.

    A view is marked degraded when compensation has ALREADY failed. Refusing to record that
    because the generation moved would leave the fault invisible, which is the opposite of what
    the mark is for.
    """
    publish_root(
        repo, project_id=pid, view_key=VIEW, root_id="root-1", expected_generation=0
    )
    publish_root(
        repo, project_id=pid, view_key=VIEW, root_id="root-2", expected_generation=1
    )

    marked = mark_degraded(repo, project_id=pid, view_key=VIEW, reason="late")

    assert marked.degraded is True
