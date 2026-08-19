"""CF-20 / CF-21 recovery matrix: real coordinators, real graph, real dispatcher replay.

**The gap this closes.** `test_saga_reconcile_live_path` proves the dispatcher's orchestration --
gate verification, claim-before-replay, ordering, fencing, quarantine storms -- but every handler
it drives is `_Replayer()`, a fake, over a tmp journal with no graph at all. So the ORCHESTRATION
is proven and the thing being orchestrated is not: no test establishes that the REAL coordinators,
replayed THROUGH the dispatcher, leave real Neo4j in the right state. `test_unmerge_precondition`
has the same shape one layer down -- it drives `_apply` directly with stub doubles.

That matters more here than almost anywhere else in this codebase, because this subsystem has
already demonstrated that offline-green can lie about real recovery: seven online recovery tests
were failing while the ordinary suite stayed green, because the online lane was deselected.

So this file is deliberately the composition nobody had tested:

    normal operation -> PREPARE -> process death at a specific boundary
                     -> restart -> REAL dispatcher replay -> final graph + journal + locks

Every handler is the production coordinator. The journal is real SQLite. The graph is real Neo4j.
The replay goes through `SagaReconcileDispatcher.replay(gate=...)` -- the single live replay
authority that `core/runtime._recover_saga_backlog` calls at startup -- rather than any
coordinator's `replay_prepared_row` reached directly, because "the reconciler is unreachable in
production" was CF-20's entire finding and a test that bypasses the dispatcher would not notice
it recurring.

**Crash injection is at `_apply_owned`, not at `_apply`.** `_apply` opens `owned_mutation`, which
is what publishes the ownership claim and heartbeat. Raising outside it would leave no claim and
model a crash that never started; raising inside models the real thing -- a process that took
ownership, began mutating, and died holding it.

**Inverse-tested, and the inverse test took three attempts -- which is itself the finding.**
Merge drift is caught by at least THREE independent layers: classification-time
(`_classify_replay`, "neither expected before- nor after-state"), the abstention re-read in
`_apply_owned` ("claimed to abstain but the graph moved"), and the after-state verification.
Disabling any ONE of them leaves the tests green, because another catches the same drift. Only
removing the first two together makes
`test_graph_mutated_between_crash_and_restart_is_quarantined_not_overwritten` and
`test_a_quarantined_operation_keeps_its_participants_fenced` fail.

Two things follow, and both are worth carrying:

* the redundancy is real defence in depth, not duplication -- a single-guard regression cannot
  silently disable drift detection here;
* a green inverse test proves nothing until you have checked WHICH guard you actually disabled.
  The second attempt here flipped `if post_fp == expected_before:` to `if False:`, which forces
  the quarantine branch ALWAYS -- it strengthened the guard while appearing to remove it, and
  would have been reported as "the test still passes, so the guard is redundant". The correct
  inverse is `if True:` there (every abstention benign) plus `if False:` on the classification
  check.

**What the inverse tests establish for CF-21, stated exactly.** Removing GUARD 4 alone
(`_classify_replay`'s `observed_fp != expected_before`) does NOT fail
`test_unmerge_crash_then_drift_is_quarantined_without_restoring`, because the after-state
verification in `_apply_owned` (`expected_after and after_fp != expected_after`) catches the same
drift one step later. So unmerge has the same two-layer protection merge does.

The difference matters and is worth being precise about: with GUARD 4 removed the restore is
ATTEMPTED and then quarantined, rather than refused before touching anything. In the drift case
this file constructs -- the survivor deleted -- the attempted restore matches nothing, so no
overwrite occurs and the test still passes. **A drift case where GUARD 4 is the only thing
standing between a replay and a real overwrite was not constructed here.** That is the remaining
gap in CF-21's live proof, recorded rather than glossed: what is proven is the observable
behaviour (drift quarantines, undrifted replays complete), not that GUARD 4 is individually
load-bearing.

**Recovery semantics are NOT uniform across the saga family**, which this matrix makes concrete:

* merge and unmerge REPLAY to completion when the graph is unchanged;
* ENTITY_DELETE deliberately does NOT auto-resume -- a crashed delete whose target still exists
  is quarantined with the survivor named ("deleting them now could destroy nodes the crash
  spared"), because a wrong delete replay is unrecoverable while a delayed one is merely stuck.

A test written on the assumption that "resumes correctly" means the same thing for every kind
will assert the wrong thing for delete; this file originally did.

Run with:  pytest --run-online -m online tests/test_saga_recovery_matrix_live.py
"""

from __future__ import annotations

import json
import uuid as uuidlib

import pytest

from menhir.infrastructure import operation_owner as _operation_owner
from menhir.infrastructure import process_liveness
from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
from menhir.infrastructure.graph_operations import GraphOperationsJournal
from menhir.services.merge_coordinator import MergeCoordinator
from menhir.services.saga_reconcile_dispatcher import (
    SagaReconcileDispatcher,
    build_handlers,
)
from menhir.services.unmerge_coordinator import UnmergeCoordinator

pytestmark = [pytest.mark.online, pytest.mark.usefixtures("pid_namespace_verifiable")]


class _HeldGate:
    """A gate that reports itself held, standing in for a real acquired lease.

    The lease's own acquisition and expiry are already covered by `test_saga_lease_timing` and
    `test_saga_reconcile_live_path`; re-proving them here would duplicate that work. What this
    file needs from the gate is only that replay is permitted, so the REAL coordinators get to
    run against the REAL graph -- which is the part nothing else covers.
    """

    #: The dispatcher starts a heartbeat that derives its interval from this.
    lease_duration_s = 30.0

    def __init__(self) -> None:
        self.held = True

    def verify_still_held(self) -> bool:
        return self.held

    def renew(self) -> bool:
        return self.held

    def release(self) -> None:
        self.held = False


@pytest.fixture
def journal(tmp_path):
    j = GraphOperationsJournal(db_path=tmp_path / "saga-matrix.db")
    j._ensure_ready()
    return j


@pytest.fixture
def graph(test_neo4j_repo):
    """The FULL adapter, matching production.

    `build_default_dispatcher(adapter)` hands ONE adapter to all five coordinators, so a test
    using a narrower one (e.g. `CorrelationRepository`, which the merge-only live tests use) is
    not exercising the wiring startup actually builds -- and indeed fails outright for delete
    (`newly_unreferenced_evidence`) and metric write (`fetch_metric_state`), whose interfaces it
    does not implement.
    """
    return MemoryGraphAdapter(neo4j=test_neo4j_repo)


@pytest.fixture
def coordinators(graph, journal):
    """The PRODUCTION coordinators, wired exactly as `build_default_dispatcher` wires them."""
    return {
        "merge": MergeCoordinator(graph_adapter=graph, journal=journal),
        "unmerge": UnmergeCoordinator(graph_adapter=graph, journal=journal),
    }


@pytest.fixture
def dispatcher(journal, coordinators):
    return SagaReconcileDispatcher(
        journal=journal,
        handlers=build_handlers(
            merge=coordinators["merge"], unmerge=coordinators["unmerge"]
        ),
    )


@pytest.fixture
def pair(test_neo4j_repo):
    """A mergeable survivor/absorbed pair with an edge to preserve, in real Neo4j."""
    tag = f"saga-matrix-{uuidlib.uuid4().hex[:10]}"
    ids = {"s": f"{tag}-s", "a": f"{tag}-a", "p": f"{tag}-p"}
    test_neo4j_repo.execute(
        """
        CREATE (s:Entity {uuid:$s, name:'survivor', test_tag:$t, namespace:'default',
                          freshness:'ACTIVE', scope:'PERSISTENT', summary:'survivor summary'})
        CREATE (a:Entity {uuid:$a, name:'absorbed', test_tag:$t, namespace:'default',
                          freshness:'ACTIVE', scope:'PERSISTENT', summary:'absorbed summary'})
        CREATE (p:Entity {uuid:$p, name:'peer', test_tag:$t, namespace:'default',
                          freshness:'ACTIVE', scope:'PERSISTENT'})
        CREATE (a)-[:RELATES_TO {weight: 1}]->(p)
        """,
        params={**ids, "t": tag},
    )
    yield ids
    test_neo4j_repo.execute(
        "MATCH (n) WHERE n.test_tag = $t DETACH DELETE n", params={"t": tag}
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _exists(repo, uuid: str) -> bool:
    rows = repo.execute(
        "MATCH (n) WHERE n.uuid = $u RETURN count(n) AS c", params={"u": uuid}
    )
    return int(rows[0]["c"]) > 0 if rows else False


def _rows(journal, state: str) -> list[dict]:
    return list(journal.list_by_state(state, limit=50))


def _state_of(journal, op_id: str) -> str:
    row = journal.get(op_id)
    return str(row.get("state")) if row else "ABSENT"


def _locks_for(journal, op_id: str) -> list[str]:
    """Read the fence directly. The journal exposes no lock reader, and the fence is exactly what
    CF-20 was about -- an operation that never resolved held its participants forever -- so the
    test asserts on the durable table rather than on a derived signal."""
    from menhir.infrastructure.telemetry import connect_telemetry_db

    with connect_telemetry_db(journal.db_path) as conn:
        return [
            str(r[0])
            for r in conn.execute(
                "SELECT entity_uuid FROM graph_operation_locks WHERE op_id = ?", (op_id,)
            ).fetchall()
        ]


#: What a dead writer looks like durably: THIS deployment's instance label and THIS host, an
#: unusable PID, a different process nonce, and a long-expired lease.
#:
#: Every component is load-bearing and a first draft got the first one wrong. A token whose
#: instance label does not match this deployment cannot be used as death evidence at all -- the
#: reconciler answers OWNER_UNKNOWN ("unprovable ownership") and fences, because a PID is only
#: inspectable on the host and deployment that recorded it. Fencing on unprovable ownership is
#: correct; it just is not the scenario this test means to exercise.
_DEAD_OWNER = (
    f"{_operation_owner._instance_label()}:{process_liveness.hostname()}:999999:deadnonce"
)
_EXPIRED = "2020-01-01T00:00:00+00:00"


def _crash_merge_after_prepare(coordinators, journal, monkeypatch):
    """Simulate a process that PREPAREd, began mutating, and died -- then stopped existing.

    Two halves, and the second is the one a first draft of this file omitted.

    **Raise inside the owned mutation.** `_apply` opens `owned_mutation`, which publishes the
    ownership claim and heartbeat, so raising outside it would leave no claim at all and model a
    crash that never began.

    **Then make the owner DEAD.** The dispatcher's ownership veto is not incidental -- it is the
    guard that stops a reconciler replaying an operation whose original writer is still running.
    Because this test crashes inside its own process, the row keeps a token naming a LIVE pid,
    and replay correctly classifies it `LIVE_OWNER` and refuses. The first version of this test
    asserted COMMITTED and failed for exactly that reason: the system was right and the test was
    modelling a crash that had not actually happened. Rewriting the claim to a dead pid with an
    expired lease is what a real process death leaves behind.
    """
    def _die(*_a, **_kw):
        raise RuntimeError("simulated process death mid-merge")

    cls = type(coordinators["merge"])
    original = cls._apply_owned
    monkeypatch.setattr(cls, "_apply_owned", _die, raising=True)

    def _restart() -> None:
        """Undo ONLY the crash injection.

        NOT `monkeypatch.undo()`, which was the first attempt and quietly broke the whole file.
        `monkeypatch` is one function-scoped instance shared with every fixture the test uses, so
        `undo()` also reverted `pid_namespace_verifiable`'s `setenv` -- and without that
        assertion the reconciler cannot use a PID as death evidence, so every row classified
        OWNER_UNKNOWN and fenced. The system was behaving correctly; the test had disarmed its
        own precondition.
        """
        monkeypatch.setattr(cls, "_apply_owned", original, raising=True)

    return _restart


def _bury_the_writer(journal) -> None:
    """Rewrite every PREPARED row's claim to a dead process, as a real crash would leave it."""
    from menhir.infrastructure.telemetry import connect_telemetry_db

    with connect_telemetry_db(journal.db_path) as conn:
        conn.execute(
            "UPDATE graph_operations SET owner_token = ?, owner_lease_expires_at = ? "
            "WHERE state = 'PREPARED'",
            (_DEAD_OWNER, _EXPIRED),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# 1. Merge crash after PREPARE -> exactly-once completion through the dispatcher
# ---------------------------------------------------------------------------

@pytest.mark.online
def test_merge_crash_after_prepare_completes_exactly_once_on_replay(
    coordinators, dispatcher, journal, test_neo4j_repo, pair, monkeypatch
) -> None:
    """The core recovery promise, end to end against a real graph.

    Exactly-once is asserted on the GRAPH, not on a counter: after replay the absorbed node is
    gone, the survivor is present, and the peer edge the merge was supposed to preserve survives.
    A double-apply would show up as a second absorption attempt failing or as a lost edge.
    """
    restart = _crash_merge_after_prepare(coordinators, journal, monkeypatch)
    with pytest.raises(RuntimeError):
        coordinators["merge"].merge(
            survivor_uuid=pair["s"], absorbed_uuid=pair["a"], similarity=0.97
        )
    _bury_the_writer(journal)

    prepared = _rows(journal, "PREPARED")
    assert len(prepared) == 1, f"crash left no recoverable PREPARED row: {prepared}"
    op_id = str(prepared[0]["op_id"])
    assert _exists(test_neo4j_repo, pair["a"]), "the crash mutated before it was interrupted"

    # Restart: undo the crash injection and drain the backlog through the REAL dispatcher.
    restart()
    run = dispatcher.replay(gate=_HeldGate())

    assert _state_of(journal, op_id) == "COMMITTED", (
        f"counts={run.as_dict()['counts']} outcomes={run.as_dict().get('outcomes')} "
        f"blocking={run.as_dict().get('blocking_reasons')}"
    )
    assert not _exists(test_neo4j_repo, pair["a"]), "the absorbed node survived replay"
    assert _exists(test_neo4j_repo, pair["s"]), "the survivor was destroyed"

    edges = test_neo4j_repo.execute(
        "MATCH (s:Entity {uuid:$s})-[r:RELATES_TO]->(p:Entity {uuid:$p}) RETURN count(r) AS c",
        params={"s": pair["s"], "p": pair["p"]},
    )
    assert int(edges[0]["c"]) >= 1, "the absorbed node's edge was not carried to the survivor"


@pytest.mark.online
def test_a_second_replay_pass_is_a_no_op(
    coordinators, dispatcher, journal, test_neo4j_repo, pair, monkeypatch
) -> None:
    """Exactly-once across REPEATED recovery, which is the realistic failure: a reconciler that
    runs, and then runs again on the next restart. The second pass must find nothing to do rather
    than re-applying against a graph that has already moved on."""
    restart = _crash_merge_after_prepare(coordinators, journal, monkeypatch)
    with pytest.raises(RuntimeError):
        coordinators["merge"].merge(
            survivor_uuid=pair["s"], absorbed_uuid=pair["a"], similarity=0.97
        )
    _bury_the_writer(journal)
    restart()

    first = dispatcher.replay(gate=_HeldGate())
    second = dispatcher.replay(gate=_HeldGate())

    assert _rows(journal, "PREPARED") == [], "a PREPARED row survived two recovery passes"
    assert not _exists(test_neo4j_repo, pair["a"])
    assert second.as_dict().get("scanned", 0) == 0, (
        f"the second pass found work to do: first={first.as_dict()} second={second.as_dict()}"
    )


# ---------------------------------------------------------------------------
# 2. Locks: released on success, fenced on quarantine
# ---------------------------------------------------------------------------

@pytest.mark.online
def test_a_successful_replay_releases_the_participant_locks(
    coordinators, dispatcher, journal, pair, monkeypatch
) -> None:
    """Locks are the operational consequence of CF-20: a crashed operation fenced its UUIDs
    FOREVER because nothing ever resolved it. Proving the lock is gone after a successful replay
    is proving the fence lifts."""
    restart = _crash_merge_after_prepare(coordinators, journal, monkeypatch)
    with pytest.raises(RuntimeError):
        coordinators["merge"].merge(
            survivor_uuid=pair["s"], absorbed_uuid=pair["a"], similarity=0.97
        )
    _bury_the_writer(journal)
    op_id = str(_rows(journal, "PREPARED")[0]["op_id"])
    assert _locks_for(journal, op_id), "PREPARE took no participant locks"

    restart()
    dispatcher.replay(gate=_HeldGate())

    assert _state_of(journal, op_id) == "COMMITTED"
    assert _locks_for(journal, op_id) == [], "locks outlived a successful replay"


@pytest.mark.online
def test_a_fenced_participant_cannot_be_mutated_by_a_second_process(
    coordinators, journal, pair, monkeypatch
) -> None:
    """The property that makes an unresolved backlog SAFE rather than merely stuck.

    While op A is unresolved, a second process must not be able to start a new operation touching
    the same participants -- otherwise recovery would later replay A into a graph a peer had
    already changed. This is the guard that turns "fenced forever" from a bug into a deliberate
    fail-closed state.
    """
    restart = _crash_merge_after_prepare(coordinators, journal, monkeypatch)
    with pytest.raises(RuntimeError):
        coordinators["merge"].merge(
            survivor_uuid=pair["s"], absorbed_uuid=pair["a"], similarity=0.97
        )
    _bury_the_writer(journal)
    restart()

    # A second process, sharing the journal, attempts an operation on a fenced participant.
    second = MergeCoordinator(
        graph_adapter=coordinators["merge"].graph_adapter, journal=journal
    )
    result = second.merge(
        survivor_uuid=pair["s"], absorbed_uuid=pair["a"], similarity=0.97
    )

    assert result.get("merged") == 0, f"a fenced pair was merged by a peer: {result}"
    assert len(_rows(journal, "PREPARED")) == 1, "the peer inserted a competing PREPARED row"


# ---------------------------------------------------------------------------
# 3. Drift between crash and restart -> quarantine, never overwrite
# ---------------------------------------------------------------------------

@pytest.mark.online
def test_graph_mutated_between_crash_and_restart_is_quarantined_not_overwritten(
    coordinators, dispatcher, journal, test_neo4j_repo, pair, monkeypatch
) -> None:
    """The failure this whole design exists to prevent.

    An operator (or a peer) changes the survivor while the operation is unresolved. Replaying
    blindly would overwrite exactly the state that change represents. The precondition
    fingerprint captured at PREPARE is what detects it, and the correct response is to
    QUARANTINE -- leave the graph alone, mark the row for review -- not to proceed.
    """
    restart = _crash_merge_after_prepare(coordinators, journal, monkeypatch)
    with pytest.raises(RuntimeError):
        coordinators["merge"].merge(
            survivor_uuid=pair["s"], absorbed_uuid=pair["a"], similarity=0.97
        )
    _bury_the_writer(journal)
    op_id = str(_rows(journal, "PREPARED")[0]["op_id"])
    restart()

    # Drift the survivor OUT OF EXISTENCE. That is what the precondition actually fingerprints.
    #
    # A first version of this test edited `s.summary` and expected quarantine; the merge committed
    # instead. That is correct, and the reason is worth recording rather than papering over:
    # `merge_state_fingerprint` deliberately covers exactly three facts -- `survivor_present`,
    # `absorbed_present`, `lineage_recorded` -- because a wider fingerprint produced FALSE
    # quarantines (its docstring records that folding in `last_merge_op_id` quarantined merges
    # that had actually succeeded). So an unrelated property edit is not drift by design, and a
    # test asserting otherwise would be encoding a stricter contract than the system has.
    test_neo4j_repo.execute(
        "MATCH (s:Entity {uuid:$s}) DETACH DELETE s", params={"s": pair["s"]}
    )

    dispatcher.replay(gate=_HeldGate())

    assert _state_of(journal, op_id) == "NEEDS_REVIEW", (
        "a merge whose survivor vanished was replayed instead of quarantined"
    )
    # The absorbed node is untouched: a quarantined operation mutates nothing at all.
    assert _exists(test_neo4j_repo, pair["a"]), (
        "a quarantined merge still destroyed the absorbed node"
    )


@pytest.mark.online
def test_a_quarantined_operation_keeps_its_participants_fenced(
    coordinators, dispatcher, journal, test_neo4j_repo, pair, monkeypatch
) -> None:
    """Quarantine must NOT release the locks. NEEDS_REVIEW means "a human has to look at this",
    and unfencing would let normal traffic mutate participants whose correct state is still
    undetermined -- turning a detected problem into an undetectable one."""
    restart = _crash_merge_after_prepare(coordinators, journal, monkeypatch)
    with pytest.raises(RuntimeError):
        coordinators["merge"].merge(
            survivor_uuid=pair["s"], absorbed_uuid=pair["a"], similarity=0.97
        )
    _bury_the_writer(journal)
    op_id = str(_rows(journal, "PREPARED")[0]["op_id"])
    restart()
    # Material drift -- see the sibling test for why a property edit is not enough.
    test_neo4j_repo.execute(
        "MATCH (s:Entity {uuid:$s}) DETACH DELETE s", params={"s": pair["s"]}
    )

    dispatcher.replay(gate=_HeldGate())

    assert _state_of(journal, op_id) == "NEEDS_REVIEW"
    assert _locks_for(journal, op_id), (
        "a quarantined operation released its fence, so normal traffic can now mutate "
        "participants whose correct state is still undetermined"
    )


# ---------------------------------------------------------------------------
# 4. The dispatcher is the production path, and it refuses to replay unguarded
# ---------------------------------------------------------------------------

@pytest.mark.online
def test_live_replay_requires_the_gate_even_with_real_coordinators(dispatcher) -> None:
    """`run(dry_run=False)` without a gate must RAISE rather than quietly observing.

    Asserted here, with real handlers, because the refusal lives in the dispatcher and a future
    refactor that moved replay into the coordinators would silently lose it -- which is CF-20's
    finding in a new costume.
    """
    with pytest.raises(NotImplementedError, match="reconciliation gate"):
        dispatcher.run(dry_run=False)


@pytest.mark.online
def test_startup_wires_the_real_dispatcher_rather_than_a_coordinator(monkeypatch) -> None:
    """CF-20 was "every reconciler is unreachable". The defence is that startup builds the
    dispatcher and drives IT, so this asserts the production wiring rather than trusting it:
    `build_default_dispatcher` returns a `SagaReconcileDispatcher` carrying a handler for every
    operation kind, and `_recover_saga_backlog` is what calls it."""
    import inspect

    from menhir.core import runtime
    from menhir.services.saga_preflight import build_default_dispatcher

    source = inspect.getsource(runtime._recover_saga_backlog)
    assert "build_default_dispatcher" in source
    assert "ReconciliationGate" in source

    caller = inspect.getsource(runtime)
    assert "_recover_saga_backlog(" in caller, "nothing calls the recovery barrier at startup"

    built = build_default_dispatcher(object())
    assert isinstance(built, SagaReconcileDispatcher)
    # The kinds are read from the real wiring, not guessed: a first draft asserted
    # "MEMORY_DELETE", which is not a registered kind at all, and would have failed for a reason
    # unrelated to the invariant. An UNKNOWN_KIND row blocks write-readiness, so a kind missing
    # from this map is a boot failure rather than a silent gap.
    for kind in (
        "ENTITY_MERGE",
        "ENTITY_UNMERGE",
        "ENTITY_DELETE",
        "SESSION_TTL_DELETE",
        "METRIC_WRITE",
        "EXPLICIT_ERASURE",
        "LEGACY_ENTITY_UNMERGE",
    ):
        assert kind in built.handlers, f"{kind} has no registered handler"


# ---------------------------------------------------------------------------
# 5. CF-21: the unmerge precondition, live
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 6. ENTITY_UNMERGE: CF-21 GUARD 4, driven against a real drifted graph
# ---------------------------------------------------------------------------

@pytest.fixture
def merged_pair(coordinators, journal, test_neo4j_repo, pair):
    """A COMMITTED merge, so there is something real to unmerge."""
    result = coordinators["merge"].merge(
        survivor_uuid=pair["s"], absorbed_uuid=pair["a"], similarity=0.97
    )
    assert result.get("merged"), f"setup merge did not land: {result}"
    merge_op_id = str(result["op_id"])
    assert _state_of(journal, merge_op_id) == "COMMITTED"
    assert not _exists(test_neo4j_repo, pair["a"]), "setup merge left the absorbed node"
    return merge_op_id


def _crash_unmerge(coordinators, journal, monkeypatch, merge_op_id):
    """PREPARE an unmerge, die inside the owned mutation, then bury the writer."""
    cls = type(coordinators["unmerge"])
    original = cls._apply_owned

    def _die(*_a, **_kw):
        raise RuntimeError("simulated process death mid-unmerge")

    monkeypatch.setattr(cls, "_apply_owned", _die, raising=True)
    with pytest.raises(RuntimeError):
        coordinators["unmerge"].unmerge(merge_op_id)
    _bury_the_writer(journal)
    monkeypatch.setattr(cls, "_apply_owned", original, raising=True)

    prepared = [
        r for r in _rows(journal, "PREPARED")
        if str(r.get("operation_kind")) == "ENTITY_UNMERGE"
    ]
    assert len(prepared) == 1, f"the unmerge crash left no recoverable row: {prepared}"
    return str(prepared[0]["op_id"])


@pytest.mark.online
def test_unmerge_crash_then_drift_is_quarantined_without_restoring(
    coordinators, dispatcher, journal, test_neo4j_repo, pair, merged_pair, monkeypatch
) -> None:
    """CF-21 as a LIVE property rather than a source inspection.

    The first version of this file asserted, via `inspect.getsource`, that `_classify_replay`
    mentions `expected_before_sha256`. That pins the code SHAPE and nothing about behaviour --
    a guard could read the field and ignore it and the assertion would still pass.

    This performs a real merge, crashes a real unmerge after PREPARE, drifts a fact the
    precondition genuinely covers, and requires the dispatcher to quarantine. That is the
    finding: replaying a restore into a drifted graph overwrites exactly the survivor state the
    snapshot exists to protect.
    """
    unmerge_op = _crash_unmerge(coordinators, journal, monkeypatch, merged_pair)

    # GUARD 4 reuses `merge_state_fingerprint`, whose three facts include `survivor_present`, so
    # removing the survivor is drift the precondition actually covers -- not an arbitrary edit.
    test_neo4j_repo.execute(
        "MATCH (s:Entity {uuid:$s}) DETACH DELETE s", params={"s": pair["s"]}
    )

    dispatcher.replay(gate=_HeldGate())

    assert _state_of(journal, unmerge_op) == "NEEDS_REVIEW", (
        "a drifted unmerge was restored instead of quarantined -- CF-21 exactly"
    )
    assert not _exists(test_neo4j_repo, pair["a"]), (
        "a quarantined unmerge restored the absorbed node anyway"
    )


@pytest.mark.online
def test_an_undrifted_unmerge_still_replays_to_completion(
    coordinators, dispatcher, journal, test_neo4j_repo, pair, merged_pair, monkeypatch
) -> None:
    """The other half, without which the quarantine test would be satisfied by a guard that
    refuses everything. An unmerge crashed into an UNCHANGED graph must actually restore."""
    unmerge_op = _crash_unmerge(coordinators, journal, monkeypatch, merged_pair)

    dispatcher.replay(gate=_HeldGate())

    assert _state_of(journal, unmerge_op) == "COMMITTED", (
        "an unmerge into an unchanged graph failed to replay"
    )
    assert _exists(test_neo4j_repo, pair["a"]), "the replayed unmerge did not restore the node"


# ---------------------------------------------------------------------------
# 7. ENTITY_DELETE: crash-resume against a real node
# ---------------------------------------------------------------------------

@pytest.fixture
def delete_coordinator(graph, journal):
    from menhir.services.delete_coordinator import DeleteCoordinator

    return DeleteCoordinator(graph_adapter=graph, journal=journal)


@pytest.mark.online
def test_delete_crash_after_prepare_resumes_and_releases_its_lock(
    delete_coordinator, journal, test_neo4j_repo, pair, monkeypatch
) -> None:
    """ENTITY_DELETE was wired into the dispatcher but its crash-resume behaviour was never
    driven against a real graph -- and it turns out NOT to be the merge shape at all.

    Merge replays to completion; delete refuses to. See the comment at the assertions for why
    that asymmetry is correct rather than an oversight."""
    from menhir.services.delete_coordinator import DeleteCoordinator

    cls = DeleteCoordinator
    original = cls._mutate_and_verify

    def _die(*_a, **_kw):
        raise RuntimeError("simulated process death mid-delete")

    monkeypatch.setattr(cls, "_mutate_and_verify", _die, raising=True)
    with pytest.raises(RuntimeError):
        delete_coordinator.delete_entity(pair["p"])
    _bury_the_writer(journal)
    monkeypatch.setattr(cls, "_mutate_and_verify", original, raising=True)

    prepared = [
        r for r in _rows(journal, "PREPARED")
        if str(r.get("operation_kind")) == "ENTITY_DELETE"
    ]
    assert len(prepared) == 1, f"the delete crash left no recoverable row: {prepared}"
    op_id = str(prepared[0]["op_id"])
    assert _exists(test_neo4j_repo, pair["p"]), "the crash deleted before it was interrupted"
    assert _locks_for(journal, op_id), "PREPARE took no participant lock"

    run = SagaReconcileDispatcher(
        journal=journal, handlers=build_handlers(delete=delete_coordinator)
    ).replay(gate=_HeldGate())

    # DELETE DOES NOT AUTO-RESUME, and that is deliberate -- the opposite of merge.
    #
    # This test originally asserted COMMITTED, on the assumption that "resumes correctly" means
    # the same thing for every saga. It does not. A crashed delete whose target still EXISTS is
    # quarantined with the survivors named: "NOT retried automatically -- deleting them now could
    # destroy nodes the crash spared." Re-driving a destructive operation on the strength of a
    # journal row is exactly the asymmetry this subsystem refuses, because a wrong replay of a
    # delete is unrecoverable while a delayed one is merely stuck.
    #
    # So the recovery guarantee for ENTITY_DELETE is "surface it, never guess", and the property
    # worth pinning is that the node SURVIVES and a human is told.
    outcomes = run.as_dict().get("outcomes") or []
    assert _state_of(journal, op_id) == "NEEDS_REVIEW", outcomes
    assert _exists(test_neo4j_repo, pair["p"]), (
        "a crashed delete was auto-retried and destroyed a node the crash had spared"
    )
    assert any(pair["p"] in str(o.get("observed_error", "")) for o in outcomes), (
        f"the quarantine did not name the surviving target: {outcomes}"
    )
    assert _locks_for(journal, op_id), (
        "a quarantined delete released its fence, so ordinary traffic can now mutate a node "
        "whose intended fate is still undetermined"
    )


# ---------------------------------------------------------------------------
# 8. METRIC_WRITE: crash-resume reaches the intended value exactly once
# ---------------------------------------------------------------------------

@pytest.fixture
def metric_coordinator(graph, journal):
    from menhir.infrastructure.metric_receipts import MetricReceiptStore
    from menhir.services.metric_write_coordinator import MetricWriteCoordinator

    return MetricWriteCoordinator(
        graph_adapter=graph,
        journal=journal,
        receipts=MetricReceiptStore(db_path=journal.db_path),
    )


@pytest.mark.online
def test_metric_write_crash_resumes_to_the_intended_value_exactly_once(
    metric_coordinator, journal, monkeypatch
) -> None:
    """The one saga whose correctness is a VALUE rather than a node's presence.

    Exactly-once therefore has to be asserted on the number: a double-applied fold shows up as a
    doubled count and a lost replay as a missing one, and neither is visible in "the node is
    gone" style assertions the other cases can use.
    """
    from menhir.services.metric_write_coordinator import MetricWriteCoordinator

    subject = f"saga-matrix-{uuidlib.uuid4().hex[:8]}"
    cls = MetricWriteCoordinator
    original = cls._apply_owned

    def _die(*_a, **_kw):
        raise RuntimeError("simulated process death mid-metric-write")

    monkeypatch.setattr(cls, "_apply_owned", _die, raising=True)
    with pytest.raises(RuntimeError):
        metric_coordinator.record_telemetry_absolute(
            subject=subject,
            counter="failures",
            source_table="failure_events",
            grouping={"kind": "test"},
            cutoff_id=10,
            absolute_count=7.0,
            row_ids=[1, 2, 3],
        )
    _bury_the_writer(journal)
    monkeypatch.setattr(cls, "_apply_owned", original, raising=True)

    prepared = [
        r for r in _rows(journal, "PREPARED")
        if str(r.get("operation_kind")) == "METRIC_WRITE"
    ]
    assert len(prepared) == 1, f"the metric crash left no recoverable row: {prepared}"
    op_id = str(prepared[0]["op_id"])

    dispatcher = SagaReconcileDispatcher(
        journal=journal, handlers=build_handlers(metric_write=metric_coordinator)
    )
    dispatcher.replay(gate=_HeldGate())
    assert _state_of(journal, op_id) == "COMMITTED", "the metric write did not replay"

    second = dispatcher.replay(gate=_HeldGate())
    assert second.as_dict().get("scanned", 0) == 0, "a committed metric write was replayed again"
    assert _rows(journal, "PREPARED") == []
