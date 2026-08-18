"""CF-20c: the live replay pass, and the order of operations that makes it safe.

The forecast pass (``observe``) only has to classify. The live pass has to classify, prove it may
act, take the row, and only then mutate -- and the ORDER is the entire safety argument. These tests
pin that order, because every reordering of it is a plausible-looking refactor that reintroduces a
double-apply:

* replaying before claiming lets two reconcilers both mutate and discover it at the journal;
* checking the gate once at the top lets a pass outlive its lease and keep mutating;
* trusting the advisory classification instead of the claim's own transaction reintroduces the
  read-then-write race the claim exists to close.

The distinction between row-local and systemic failure is pinned here too. One irreconcilable
operation must not block recovery of the rest; lease loss or a quarantine storm must stop the pass
AND keep the writer gate closed -- never "stop recovery and start normally".
"""

from __future__ import annotations

import sqlite3
import threading
from time import monotonic, sleep

import pytest

from menhir.infrastructure import operation_owner as oo
from menhir.infrastructure import process_liveness
from menhir.infrastructure.graph_operations import GraphOperationsJournal
from menhir.services.saga_reconcile_gate import GateHeartbeat, ReconciliationLeaseLost
from menhir.services.saga_reconcile_dispatcher import (
    LEGACY_UNMERGE_DISPOSITION,
    SagaReconcileDispatcher,
    build_handlers,
)
from menhir.services.saga_reconcile_outcomes import (
    DRIFTED,
    FAILED,
    LIVE_OWNER,
    OWNER_UNKNOWN,
    REPLAYED,
    UNKNOWN_KIND,
)

_DEAD_LOCAL = f"inst:{process_liveness.hostname()}:999999:deadnonce"
_PAST = "2020-01-01T00:00:00+00:00"
_FUTURE = "2999-01-01T00:00:00+00:00"

#: The PID-evidence path needs the deployment assertion, or every expired row fences at
#: OWNER_UNKNOWN and these tests would be exercising the wrong branch.
pytestmark = pytest.mark.usefixtures("pid_namespace_verifiable")


class _Replayer:
    """A handler that records the rows it was asked to replay and returns a scripted outcome."""

    def __init__(self, outcome=REPLAYED, diagnostics=None, raises=None, journal=None):
        self.outcome = outcome
        self.diagnostics = diagnostics or {}
        self.raises = raises
        self.replayed: list[str] = []
        self.journal = journal
        self.owner_at_replay: dict[str, object] = {}

    def classify_prepared_row(self, row):
        return self.outcome, dict(self.diagnostics)

    def replay_prepared_row(self, row):
        op_id = str(row.get("op_id"))
        self.replayed.append(op_id)
        # Record who owns the row AT THE MOMENT the handler runs. This is what proves the claim
        # happened first: without it the test would only show that the handler was reached, which
        # stays true even if the claim is deleted outright.
        if self.journal is not None:
            stored = self.journal.get(op_id) or {}
            self.owner_at_replay[op_id] = stored.get("owner_token")
        if self.raises is not None:
            raise self.raises
        return self.outcome, dict(self.diagnostics)


class _Gate:
    """A gate that is held until told otherwise, and can lapse after N checks.

    ``lease_duration_s`` is large so the renewal heartbeat never fires during a test; these tests
    are about the row loop's own checks, and a background thread racing them would make them
    flaky rather than more thorough. Gate-renewal behaviour is covered separately.
    """

    lease_duration_s = 3600.0

    def __init__(self, *, lose_after=None):
        self.lose_after = lose_after
        self.checks = 0
        self.renewals = 0

    def verify_still_held(self):
        self.checks += 1
        if self.lose_after is not None and self.checks > self.lose_after:
            raise ReconciliationLeaseLost("gate taken by another reconciler")

    def renew(self):
        self.renewals += 1
        return True


@pytest.fixture()
def journal(tmp_path):
    j = GraphOperationsJournal(db_path=tmp_path / "ops.db")
    j._ensure_ready()
    return j


def _insert(journal, op_id, kind="ENTITY_MERGE", *, owner="expired", created_at=None):
    token, expires = {
        "expired": (_DEAD_LOCAL, _PAST),
        "live": (_DEAD_LOCAL, _FUTURE),
        "none": (None, None),
    }[owner]
    stamp = created_at or "2026-01-01T00:00:00+00:00"
    with sqlite3.connect(journal.db_path) as conn:
        conn.execute(
            "INSERT INTO graph_operations (op_id, operation_kind, request_json, state, "
            "created_at, updated_at, owner_token, owner_lease_expires_at) "
            "VALUES (?, ?, '{}', 'PREPARED', ?, ?, ?, ?)",
            (op_id, kind, stamp, stamp, token, expires),
        )
        conn.commit()


def _state(journal, op_id):
    return journal.get(op_id)["state"]


# --------------------------------------------------------------------- the gate is mandatory


@pytest.mark.unit
def test_live_replay_without_a_gate_is_refused(journal):
    """No gate means the global PREPARE pause is not in force.

    A writer could then insert a PREPARED row after recovery began reading the backlog, and
    recovery would be draining a set that is still growing underneath it.
    """
    _insert(journal, "op-1")
    dispatcher = SagaReconcileDispatcher(
        journal=journal, handlers=build_handlers(merge=_Replayer())
    )

    with pytest.raises(NotImplementedError, match="reconciliation gate"):
        dispatcher.run(dry_run=False)

    assert _state(journal, "op-1") == "PREPARED", "a refused run must not touch anything"


@pytest.mark.unit
def test_the_gate_is_verified_before_every_row_and_again_before_the_verdict(journal):
    """The property that matters is coverage, not a count.

    One check per row leaves the LAST row's mutation uncovered: every per-row check happens before
    its own side effect, so without a final check a run can report write_ready after the PREPARE
    pause has already lapsed. Hence rows + 1.
    """
    for i in range(4):
        _insert(journal, f"op-{i}", created_at=f"2026-01-0{i + 1}T00:00:00+00:00")
    gate = _Gate()

    run = SagaReconcileDispatcher(
        journal=journal, handlers=build_handlers(merge=_Replayer())
    ).run(dry_run=False, gate=gate)

    assert gate.checks == 5, (
        f"expected one check per row plus a final pre-verdict check, got {gate.checks}"
    )
    assert run.write_ready is True


@pytest.mark.unit
def test_losing_the_gate_after_the_last_row_still_blocks_the_readiness_verdict(journal):
    """The uncovered case that made a per-row-only check insufficient.

    A single replay can begin with seconds left on the lease and outlast the whole TTL. On a
    one-row backlog there is no "next row" at which the expiry would ever be noticed, so without
    the final check the run reports success after new writers were already admitted.
    """
    _insert(journal, "op-only")

    # Held for the row's own check, gone by the time the verdict is taken.
    run = SagaReconcileDispatcher(
        journal=journal, handlers=build_handlers(merge=_Replayer())
    ).run(dry_run=False, gate=_Gate(lose_after=1))

    assert run.aborted is True
    assert "before the readiness verdict" in (run.abort_reason or "")
    assert run.write_ready is False


@pytest.mark.unit
def test_the_gate_heartbeat_latches_lost_when_renewal_fails():
    """Renewal runs on its own thread, so gate loss is discoverable without reaching a next row.

    Tested directly rather than by racing the dispatcher: driving it through a live pass would make
    the result depend on whether the backlog finished before the first renewal tick, which is
    exactly the kind of timing-dependent assertion that passes for the wrong reason.
    """
    class _LosesRenewal:
        lease_duration_s = 3.0

        def __init__(self):
            self.renewals = 0

        def renew(self):
            self.renewals += 1
            return False

    gate = _LosesRenewal()
    hb = GateHeartbeat(gate, interval_s=0.05).start()
    try:
        deadline = monotonic() + 5.0
        while not hb.lost and monotonic() < deadline:
            sleep(0.02)
    finally:
        hb.stop()

    assert gate.renewals >= 1, "the heartbeat must actually attempt renewal"
    assert hb.lost is True, "a failed renewal must latch as lost, never be retried into success"


@pytest.mark.unit
def test_the_gate_heartbeat_keeps_renewing_while_it_succeeds():
    """The contrast case, so the test above pins failure handling and not merely that it runs."""
    class _Holds:
        lease_duration_s = 3.0

        def __init__(self):
            self.renewals = 0

        def renew(self):
            self.renewals += 1
            return True

    gate = _Holds()
    hb = GateHeartbeat(gate, interval_s=0.05).start()
    try:
        deadline = monotonic() + 2.0
        while gate.renewals < 3 and monotonic() < deadline:
            sleep(0.02)
    finally:
        hb.stop()

    assert gate.renewals >= 3
    assert hb.lost is False


# --------------------------------------------------------------------- claim before mutate


@pytest.mark.unit
def test_an_abandoned_row_is_claimed_before_the_handler_sees_it(journal):
    """The claim converts "looks abandoned" into "is mine", and must precede any graph access."""
    _insert(journal, "op-1")
    handler = _Replayer(journal=journal)

    run = SagaReconcileDispatcher(
        journal=journal, handlers=build_handlers(merge=handler)
    ).run(dry_run=False, gate=_Gate())

    assert handler.replayed == ["op-1"]
    # The load-bearing assertion. Reaching the handler proves nothing on its own -- that stays
    # true even with the claim deleted. Ownership having ALREADY transferred by the time the
    # handler ran is what proves the claim preceded the mutation it authorises.
    assert handler.owner_at_replay["op-1"] == oo.process_owner_token(), (
        "the row must be owned by this process BEFORE its handler touches the graph"
    )
    assert handler.owner_at_replay["op-1"] != _DEAD_LOCAL
    assert run.counts[REPLAYED] == 1
    assert run.dry_run is False, "a live run must not report itself as a forecast"


@pytest.mark.unit
def test_a_live_writers_row_is_never_claimed_or_replayed(journal):
    """The hard veto. This is the double-apply the whole design exists to prevent."""
    _insert(journal, "op-live", owner="live")
    handler = _Replayer()

    run = SagaReconcileDispatcher(
        journal=journal, handlers=build_handlers(merge=handler)
    ).run(dry_run=False, gate=_Gate())

    assert handler.replayed == [], "a live writer's row must never reach a handler"
    assert run.counts[LIVE_OWNER] == 1
    assert journal.get("op-live")["owner_token"] == _DEAD_LOCAL, "owner must be untouched"


@pytest.mark.unit
def test_the_ownership_veto_short_circuits_before_the_claim_is_even_attempted(journal):
    """Pins the veto itself, not just its outcome.

    Deleting the veto entirely does NOT change what happens to a live row: the claim rejects it
    anyway, because a fresh lease is not abandoned. That defence-in-depth is deliberate and good,
    but it means an outcome assertion alone cannot tell whether the veto still exists. The
    observable difference is that the veto returns BEFORE any claim is attempted, so a live row
    costs no write at all -- and, more importantly, the veto is what keeps ``_classify`` and
    ``_replay_one`` agreeing about a row rather than letting them diverge silently.
    """
    _insert(journal, "op-live", owner="live")
    attempts: list[str] = []
    real_claim = journal.claim_abandoned_operation

    def _spy(op_id, **kwargs):
        attempts.append(op_id)
        return real_claim(op_id, **kwargs)

    journal.claim_abandoned_operation = _spy  # type: ignore[method-assign]
    try:
        run = SagaReconcileDispatcher(
            journal=journal, handlers=build_handlers(merge=_Replayer())
        ).run(dry_run=False, gate=_Gate())
    finally:
        journal.claim_abandoned_operation = real_claim  # type: ignore[method-assign]

    assert attempts == [], "a live owner must be vetoed before any claim is attempted"
    assert run.counts[LIVE_OWNER] == 1


@pytest.mark.unit
def test_an_ownerless_row_is_fenced_and_blocks_readiness(journal):
    """Mixed-version rollout: an older binary with no ownership support may still be running it."""
    _insert(journal, "op-legacy-owner", owner="none")
    handler = _Replayer()

    run = SagaReconcileDispatcher(
        journal=journal, handlers=build_handlers(merge=handler)
    ).run(dry_run=False, gate=_Gate())

    assert handler.replayed == []
    assert run.counts[OWNER_UNKNOWN] == 1
    assert run.write_ready is False
    assert any("unprovable ownership" in r for r in run.blocking_reasons)


@pytest.mark.unit
def test_losing_the_claim_to_another_reconciler_skips_the_row_without_error(journal, monkeypatch):
    """A normal outcome, not a failure: the other reconciler will handle it."""
    _insert(journal, "op-1")
    handler = _Replayer()
    dispatcher = SagaReconcileDispatcher(
        journal=journal, handlers=build_handlers(merge=handler)
    )
    monkeypatch.setattr(journal, "claim_abandoned_operation", lambda *a, **k: False)

    run = dispatcher.run(dry_run=False, gate=_Gate())

    assert handler.replayed == [], "no claim means no mutation"
    assert run.counts[LIVE_OWNER] == 1
    assert run.aborted is False, "another reconciler owning a row is not a systemic failure"


@pytest.mark.unit
def test_two_concurrent_live_passes_replay_a_row_at_most_once(journal):
    """The property the mechanism rests on, exercised through the real claim."""
    _insert(journal, "op-contested")
    handlers = [_Replayer(), _Replayer()]
    barrier = threading.Barrier(2)

    def _go(h):
        barrier.wait()
        SagaReconcileDispatcher(
            journal=journal, handlers=build_handlers(merge=h)
        ).run(dry_run=False, gate=_Gate())

    threads = [threading.Thread(target=_go, args=(h,)) for h in handlers]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    total = sum(len(h.replayed) for h in handlers)
    assert total == 1, f"exactly one pass may replay the row, got {total}"


# --------------------------------------------------------------------- row-local vs systemic


@pytest.mark.unit
def test_a_quarantined_row_does_not_stop_the_pass(journal):
    """Row-local. Recovery quarantining one bad operation is recovery working."""
    _insert(journal, "op-bad", created_at="2026-01-01T00:00:00+00:00")
    _insert(journal, "op-good", created_at="2026-01-02T00:00:00+00:00")
    good = _Replayer()

    class _Mixed(_Replayer):
        def replay_prepared_row(self, row):
            self.replayed.append(str(row.get("op_id")))
            if row.get("op_id") == "op-bad":
                return DRIFTED, {"observed_error": "drifted"}
            return REPLAYED, {}

    handler = _Mixed()
    run = SagaReconcileDispatcher(
        journal=journal, handlers=build_handlers(merge=handler)
    ).run(dry_run=False, gate=_Gate())

    assert handler.replayed == ["op-bad", "op-good"], "the pass must continue past a quarantine"
    assert run.counts[DRIFTED] == 1 and run.counts[REPLAYED] == 1
    assert run.write_ready is True, "one quarantined row is not a systemic verdict"
    assert good.replayed == []


@pytest.mark.unit
def test_a_quarantine_storm_aborts_and_keeps_the_writer_gate_closed(journal):
    """Systemic. The rule is never "stop recovery and start normally"."""
    for i in range(6):
        _insert(journal, f"op-{i}", created_at=f"2026-01-0{i + 1}T00:00:00+00:00")

    run = SagaReconcileDispatcher(
        journal=journal,
        handlers=build_handlers(merge=_Replayer(outcome=DRIFTED, diagnostics={"observed_error": "d"})),
        max_needs_review=2,
    ).run(dry_run=False, gate=_Gate())

    assert run.aborted is True
    assert "above the 2 ceiling" in (run.abort_reason or "")
    assert run.scanned < 6, "an aborted pass must stop, not finish the backlog"
    assert run.write_ready is False


@pytest.mark.unit
def test_losing_the_gate_mid_pass_aborts_before_the_next_side_effect(journal):
    """Another reconciler may already have taken the gate and begun replaying these same rows."""
    for i in range(4):
        _insert(journal, f"op-{i}", created_at=f"2026-01-0{i + 1}T00:00:00+00:00")
    handler = _Replayer()

    run = SagaReconcileDispatcher(
        journal=journal, handlers=build_handlers(merge=handler)
    ).run(dry_run=False, gate=_Gate(lose_after=2))

    assert run.aborted is True
    assert "gate lost mid-pass" in (run.abort_reason or "")
    assert len(handler.replayed) == 2, "no row may be replayed after the gate is gone"
    assert run.write_ready is False


@pytest.mark.unit
def test_a_failed_replay_leaves_the_row_for_a_later_pass_and_blocks_readiness(journal):
    """A transient outage must not become a permanent operator ticket, but must not be ignored."""
    _insert(journal, "op-1")
    handler = _Replayer(raises=RuntimeError("neo4j outage"))

    run = SagaReconcileDispatcher(
        journal=journal, handlers=build_handlers(merge=handler)
    ).run(dry_run=False, gate=_Gate())

    assert run.counts[FAILED] == 1
    assert _state(journal, "op-1") == "PREPARED", "a failed replay must stay retryable"
    assert run.write_ready is False
    assert any("replay failed" in r for r in run.blocking_reasons)


@pytest.mark.unit
def test_an_unknown_kind_blocks_readiness_without_being_touched(journal):
    """A row nobody can account for is exactly what must not be waved through."""
    _insert(journal, "op-weird", kind="METRIC_MIGRATE")

    run = SagaReconcileDispatcher(
        journal=journal, handlers=build_handlers(merge=_Replayer())
    ).run(dry_run=False, gate=_Gate())

    assert run.counts[UNKNOWN_KIND] == 1
    assert _state(journal, "op-weird") == "PREPARED"
    assert run.write_ready is False


# --------------------------------------------------------------------- non-replayable kinds


@pytest.mark.unit
def test_a_legacy_unmerge_row_is_quarantined_by_the_dispatcher(journal):
    """CF-209's disposition, performed. The dispatcher owns the journal, so it does the write."""
    _insert(journal, "op-legacy", kind="LEGACY_ENTITY_UNMERGE")

    run = SagaReconcileDispatcher(
        journal=journal, handlers=build_handlers(merge=_Replayer())
    ).run(dry_run=False, gate=_Gate())

    assert run.counts[DRIFTED] == 1
    assert _state(journal, "op-legacy") == "NEEDS_REVIEW"
    assert "non-replayable" in journal.get("op-legacy")["last_error"]
    assert run.counts[UNKNOWN_KIND] == 0


@pytest.mark.unit
def test_a_live_owner_still_vetoes_a_legacy_row_before_quarantine(journal):
    """Even a terminal disposition may not be applied to a row someone else still owns."""
    _insert(journal, "op-legacy", kind="LEGACY_ENTITY_UNMERGE", owner="live")

    run = SagaReconcileDispatcher(
        journal=journal, handlers=build_handlers(merge=_Replayer())
    ).run(dry_run=False, gate=_Gate())

    assert run.counts[LIVE_OWNER] == 1
    assert _state(journal, "op-legacy") == "PREPARED"
    assert LEGACY_UNMERGE_DISPOSITION.kind == "LEGACY_ENTITY_UNMERGE"


# --------------------------------------------------------------------- reporting honesty


@pytest.mark.unit
def test_a_live_summary_seeds_live_outcomes_not_forecast_ones(journal):
    """A permanent row of WOULD_* zeros in a live report reads as "checked and found none"."""
    _insert(journal, "op-1")

    run = SagaReconcileDispatcher(
        journal=journal, handlers=build_handlers(merge=_Replayer())
    ).run(dry_run=False, gate=_Gate())

    assert "WOULD_REPLAY" not in run.counts
    assert {REPLAYED, DRIFTED, FAILED, LIVE_OWNER, OWNER_UNKNOWN} <= set(run.counts)
    assert run.as_dict()["dry_run"] is False
    assert run.as_dict()["aborted"] is False
