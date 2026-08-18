"""The central dispatcher against a REAL Neo4j (CF-20b/CF-20c, online).

The offline dispatcher tests drive stub handlers over a SQLite journal. They prove the routing,
the ownership veto and the readiness verdict, but every ``classify_prepared_row`` they call is a
fake that returns whatever the test asked for. Nothing there establishes that the REAL coordinator
classifications reach the right answer when they read real graph state through the dispatcher --
which is the only thing that matters, because the dispatcher is the single live replay authority.

This closes that gap for the classification pass. It does NOT cover live replay through the
dispatcher, because ``run(dry_run=False)`` still raises: there is no such path yet.

Ownership note. A row PREPAREd inside this test carries THIS process's owner token and a fresh
lease, so the dispatcher correctly vetoes it as LIVE_OWNER -- the writer really is alive. A real
crash-and-restart produces a different token, because the process nonce is regenerated at import.
The tests below assert the veto first (it is the anti-double-apply guard, and worth pinning), then
simulate the restart the way a real one looks: a dead writer's token on the row.
"""

from __future__ import annotations

import sqlite3
import uuid as uuidlib

import pytest

from menhir.infrastructure import operation_owner as oo
from menhir.infrastructure import process_liveness
from menhir.infrastructure.correlation_queries import CorrelationRepository
from menhir.infrastructure.graph_operations import GraphOperationsJournal
from menhir.services.merge_coordinator import MergeCoordinator
from menhir.services.saga_reconcile_dispatcher import (
    SagaReconcileDispatcher,
    build_handlers,
)
from menhir.services.saga_reconcile_outcomes import (
    LIVE_OWNER,
    UNKNOWN_KIND,
    WOULD_MARK_ALREADY_APPLIED,
    WOULD_NEEDS_REVIEW,
)

#: A writer on THIS host whose PID is gone -- what a crashed-and-restarted predecessor looks like.
_DEAD_LOCAL = f"inst:{process_liveness.hostname()}:999999:deadnonce"
_PAST = "2020-01-01T00:00:00+00:00"

#: The PID-evidence path needs the deployment assertion; without it every expired row fences at
#: OWNER_UNKNOWN by design and these tests would be asserting the wrong mechanism.
pytestmark = pytest.mark.usefixtures("pid_namespace_verifiable")


@pytest.fixture
def live_repo(test_neo4j_repo):
    """The stood-up TEST instance, never the operator's real graph."""
    return test_neo4j_repo


@pytest.fixture
def journal(tmp_path):
    return GraphOperationsJournal(db_path=tmp_path / "saga.db")


@pytest.fixture
def coord(live_repo, journal):
    return MergeCoordinator(
        graph_adapter=CorrelationRepository(live_repo), journal=journal
    )


@pytest.fixture
def pair(live_repo):
    tag = f"test-saga-dispatch-{uuidlib.uuid4()}"
    ids = {"s": f"{tag}-s", "a": f"{tag}-a"}
    live_repo.execute(
        """
        CREATE (s:Entity {uuid:$s, name:'survivor', test_tag:$t, namespace:'default',
                          freshness:'ACTIVE', scope:'PERSISTENT', summary:'short'})
        CREATE (a:Entity {uuid:$a, name:'absorbed', test_tag:$t, namespace:'default',
                          freshness:'ACTIVE', scope:'PERSISTENT', summary:'short'})
        """,
        params={"s": ids["s"], "a": ids["a"], "t": tag},
    )
    return ids


def _kill_owner(journal: GraphOperationsJournal, op_id: str) -> None:
    """Make the row look like it was left by a process that has since died.

    This is what a crash-and-restart genuinely produces: a token from a process that no longer
    exists. Writing it directly is the only way to get that shape inside one process.
    """
    with sqlite3.connect(journal.db_path) as conn:
        conn.execute(
            "UPDATE graph_operations SET owner_token = ?, owner_lease_expires_at = ? "
            "WHERE op_id = ?",
            (_DEAD_LOCAL, _PAST, op_id),
        )
        conn.commit()


def _crash_before_commit(coord, pair) -> str:
    """Perform a real merge against the real graph, crashing before the journal commits."""
    def boom(op_id):
        raise RuntimeError("crash before COMMITTED")

    real = coord.journal.mark_committed
    coord.journal.mark_committed = boom  # type: ignore[method-assign]
    try:
        with pytest.raises(RuntimeError):
            coord.merge(survivor_uuid=pair["s"], absorbed_uuid=pair["a"], similarity=0.97)
    finally:
        coord.journal.mark_committed = real  # type: ignore[method-assign]

    prepared = coord.journal.list_by_state("PREPARED")
    assert len(prepared) == 1, prepared
    return str(prepared[0]["op_id"])


@pytest.mark.online
def test_a_live_writers_row_is_vetoed_before_any_graph_read(coord, journal, pair):
    """The anti-double-apply guard, against a real graph.

    The row was PREPAREd by this process, which is still running, so the dispatcher must refuse to
    reason about it at all -- its graph state is mid-flight and means nothing yet.
    """
    op_id = _crash_before_commit(coord, pair)

    run = SagaReconcileDispatcher(
        journal=journal, handlers=build_handlers(merge=coord)
    ).observe()

    assert run.scanned == 1
    assert run.rows[0]["op_id"] == op_id
    assert run.rows[0]["outcome"] == LIVE_OWNER
    assert run.rows[0]["own_claim"] is True
    assert run.write_ready is True, "a live writer is normal and transient, and must not block"


@pytest.mark.online
def test_a_dead_writers_completed_mutation_is_recognised_as_already_applied(
    coord, journal, pair
):
    """The real classification path, end to end, against a real graph.

    The merge DID land before the crash, so the graph already holds this operation's after-state.
    The real MergeCoordinator.classify_prepared_row must read that state through the dispatcher and
    say so -- no replay is needed, only a journal transition. A stub handler cannot prove this.
    """
    op_id = _crash_before_commit(coord, pair)
    _kill_owner(journal, op_id)

    run = SagaReconcileDispatcher(
        journal=journal, handlers=build_handlers(merge=coord)
    ).observe()

    assert run.rows[0]["outcome"] == WOULD_MARK_ALREADY_APPLIED, run.rows
    assert run.write_ready is True
    assert journal.get(op_id)["state"] == "PREPARED", "observe() must mutate nothing"


@pytest.mark.online
def test_drift_introduced_by_another_writer_is_classified_for_quarantine(
    coord, journal, pair, live_repo
):
    """Someone else changed the graph under a PREPARED row. Quarantine, never commit.

    This is the case a fake graph cannot honestly produce: the drift is a real node deleted out
    from under a real operation, and the verdict comes from the real fingerprint comparison.
    """
    op_id = _crash_before_commit(coord, pair)
    _kill_owner(journal, op_id)

    # A different writer removes the survivor, so the graph is now in neither expected state.
    live_repo.execute("MATCH (n:Entity {uuid:$u}) DETACH DELETE n", params={"u": pair["s"]})

    run = SagaReconcileDispatcher(
        journal=journal, handlers=build_handlers(merge=coord)
    ).observe()

    assert run.rows[0]["outcome"] == WOULD_NEEDS_REVIEW, run.rows
    assert journal.get(op_id)["state"] == "PREPARED", "observe() must mutate nothing"


@pytest.mark.online
def test_a_legacy_unmerge_row_quarantines_alongside_real_rows(coord, journal, pair):
    """CF-209 in a real backlog: the legacy row quarantines and does not block the rest.

    The offline test proves the routing. This proves it holds when the same pass is also running
    real coordinator classifications against a real graph, which is where a global blocker would
    actually have hurt.
    """
    op_id = _crash_before_commit(coord, pair)
    _kill_owner(journal, op_id)

    with sqlite3.connect(journal.db_path) as conn:
        conn.execute(
            "INSERT INTO graph_operations (op_id, operation_kind, request_json, state, "
            "created_at, updated_at, owner_token, owner_lease_expires_at) "
            "VALUES ('op-legacy', 'LEGACY_ENTITY_UNMERGE', '{}', 'PREPARED', ?, ?, ?, ?)",
            (_PAST, _PAST, _DEAD_LOCAL, _PAST),
        )
        conn.commit()

    run = SagaReconcileDispatcher(
        journal=journal, handlers=build_handlers(merge=coord)
    ).observe()

    by_id = {r["op_id"]: r for r in run.rows}
    assert by_id["op-legacy"]["outcome"] == WOULD_NEEDS_REVIEW
    assert by_id[op_id]["outcome"] == WOULD_MARK_ALREADY_APPLIED
    assert run.counts[UNKNOWN_KIND] == 0
    assert run.write_ready is True, (
        f"one quarantined legacy row must not block a healthy backlog: {run.blocking_reasons}"
    )


@pytest.mark.online
def test_live_replay_through_the_dispatcher_is_still_refused(coord, journal, pair):
    """The activation gate, asserted against a real graph rather than assumed.

    CF-20c is not finished. If this ever starts passing silently, recovery has been armed without
    the preflight that is supposed to precede it.
    """
    _crash_before_commit(coord, pair)
    dispatcher = SagaReconcileDispatcher(
        journal=journal, handlers=build_handlers(merge=coord)
    )

    with pytest.raises(NotImplementedError, match="CF-20c"):
        dispatcher.run(dry_run=False)
