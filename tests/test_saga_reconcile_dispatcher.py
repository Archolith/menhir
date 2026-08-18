"""CF-20b: the central PREPARED dispatcher, observe mode.

Uses a fake handler rather than the real coordinators: what is under test here is the dispatcher's
own behaviour -- the ownership veto, routing, unknown kinds, exhaustiveness and the readiness
verdict. The coordinators' classification is covered by their own tests, and wiring real ones in
would make these tests fail for reasons that have nothing to do with dispatch.

The journal is real, so the no-mutation claim is checked against durable state.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from menhir.infrastructure import operation_owner as oo
from menhir.infrastructure import process_liveness
from menhir.infrastructure.graph_operations import GraphOperationsJournal
from menhir.services.saga_reconcile_dispatcher import (
    SagaReconcileDispatcher,
    build_handlers,
)
from menhir.services.saga_reconcile_outcomes import (
    LIVE_OWNER,
    OWNER_UNKNOWN,
    SKIP,
    UNKNOWN_KIND,
    WOULD_NEEDS_REVIEW,
    WOULD_REPLAY,
)

_PAST = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
_FUTURE = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()


class _Handler:
    """Records which rows it was asked about and returns a scripted outcome."""

    def __init__(self, outcome=WOULD_REPLAY, diagnostics=None, raises=None):
        self.outcome = outcome
        self.diagnostics = diagnostics or {}
        self.raises = raises
        self.seen: list[str] = []

    def classify_prepared_row(self, row):
        self.seen.append(str(row.get("op_id")))
        if self.raises is not None:
            raise self.raises
        return self.outcome, dict(self.diagnostics)


@pytest.fixture()
def journal(tmp_path):
    j = GraphOperationsJournal(db_path=tmp_path / "ops.db")
    j._ensure_ready()
    return j


#: Every test below that reaches the PID-evidence path needs the deployment assertion that
#: makes a local PID lookup meaningful for a same-hostname owner. Without it the classifier
#: fences at OWNER_UNKNOWN by design; see ``operation_owner.host_pid_namespace_is_verifiable``.
pytestmark = pytest.mark.usefixtures("pid_namespace_verifiable")


def _insert(journal, op_id, kind="ENTITY_MERGE", *, owner="expired", created_at=None):
    """Insert a PREPARED row with a chosen ownership posture.

    owner="expired" -> abandoned (same host, dead PID), so it reaches a handler
    owner="live"    -> a fresh claim, so the ownership veto fires
    owner="none"    -> a legacy ownerless row
    """
    # "expired" must be a writer whose death is PROVABLE: same host, PID gone. Expiry alone is no
    # longer sufficient to reach a handler -- that is the point of the death-evidence rule.
    dead_local = f"inst:{process_liveness.hostname()}:999999:deadnonce"
    token, expires = {
        "expired": (dead_local, _PAST),
        "live": (dead_local, _FUTURE),
        "none": (None, None),
    }[owner]
    with sqlite3.connect(journal.db_path) as conn:
        conn.execute(
            "INSERT INTO graph_operations (op_id, operation_kind, request_json, state, "
            "created_at, updated_at, owner_token, owner_lease_expires_at) "
            "VALUES (?, ?, '{}', 'PREPARED', ?, ?, ?, ?)",
            (op_id, kind, created_at or "2026-01-01T00:00:00+00:00",
             created_at or "2026-01-01T00:00:00+00:00", token, expires),
        )
        conn.commit()


def _dump(db_path):
    with sqlite3.connect(db_path) as conn:
        return (
            [tuple(r) for r in conn.execute("SELECT * FROM graph_operations ORDER BY op_id")],
            [tuple(r) for r in conn.execute(
                "SELECT * FROM graph_operation_locks ORDER BY entity_uuid")],
        )


# --------------------------------------------------------------------------- ownership veto


@pytest.mark.unit
def test_a_live_owner_row_is_vetoed_before_the_handler_sees_it(journal):
    """A row another process is still executing must not reach saga logic at all.

    Its graph state is mid-flight, so a precondition comparison against it is meaningless.
    """
    _insert(journal, "op-live", owner="live")
    handler = _Handler()
    run = SagaReconcileDispatcher(
        journal=journal, handlers=build_handlers(merge=handler)
    ).observe()

    assert run.counts[LIVE_OWNER] == 1
    assert handler.seen == [], "the handler must never be consulted about a live-owned row"


@pytest.mark.unit
def test_an_ownerless_legacy_row_is_owner_unknown_not_replayable(journal):
    _insert(journal, "op-legacy", owner="none")
    handler = _Handler()
    run = SagaReconcileDispatcher(
        journal=journal, handlers=build_handlers(merge=handler)
    ).observe()

    assert run.counts[OWNER_UNKNOWN] == 1
    assert handler.seen == []


@pytest.mark.unit
def test_an_abandoned_row_reaches_its_handler(journal):
    _insert(journal, "op-abandoned", owner="expired")
    handler = _Handler(outcome=WOULD_REPLAY)
    run = SagaReconcileDispatcher(
        journal=journal, handlers=build_handlers(merge=handler)
    ).observe()

    assert handler.seen == ["op-abandoned"]
    assert run.counts[WOULD_REPLAY] == 1


# --------------------------------------------------------------------------- routing


@pytest.mark.unit
def test_legacy_entity_unmerge_is_unknown_kind_not_silently_skipped(journal):
    """The concrete gap this dispatcher closes.

    LEGACY_ENTITY_UNMERGE rows ARE written by the legacy coordinator, and no reconciler claims
    them, so today a crash leaving one PREPARED is invisible to every reconciler in the system. A
    per-coordinator scan expresses "not mine" and "not anyone's" identically, as a silent continue.
    """
    _insert(journal, "op-legacy-unmerge", kind="LEGACY_ENTITY_UNMERGE")
    run = SagaReconcileDispatcher(
        journal=journal, handlers=build_handlers(merge=_Handler())
    ).observe()

    assert run.counts[UNKNOWN_KIND] == 1
    assert run.rows[0]["outcome"] == UNKNOWN_KIND
    assert "no reconciler claims" in run.rows[0]["observed_error"]
    assert run.write_ready is False, "an unclassifiable kind must block readiness"


@pytest.mark.unit
@pytest.mark.parametrize("kind", ["METRIC_MIGRATE", "METRIC_REVERSE"])
def test_declared_but_unwritten_kinds_are_unknown_kind(journal, kind):
    """Declared in OPERATION_KINDS but no code writes or reconciles them.

    A row of either kind means something unexpected happened and must not be waved through.
    """
    _insert(journal, f"op-{kind}", kind=kind)
    run = SagaReconcileDispatcher(
        journal=journal, handlers=build_handlers(metric_write=_Handler())
    ).observe()

    assert run.counts[UNKNOWN_KIND] == 1


@pytest.mark.unit
def test_delete_kinds_both_route_to_the_delete_coordinator(journal):
    _insert(journal, "op-del", kind="ENTITY_DELETE")
    _insert(journal, "op-ttl", kind="SESSION_TTL_DELETE")
    handler = _Handler()
    SagaReconcileDispatcher(
        journal=journal, handlers=build_handlers(delete=handler)
    ).observe()

    assert sorted(handler.seen) == ["op-del", "op-ttl"]


@pytest.mark.unit
def test_each_row_is_routed_to_exactly_one_handler(journal):
    """The point of a central dispatcher: no coordinator sees another's rows."""
    _insert(journal, "op-merge", kind="ENTITY_MERGE")
    _insert(journal, "op-metric", kind="METRIC_WRITE")
    merge_handler, metric_handler = _Handler(), _Handler()
    SagaReconcileDispatcher(
        journal=journal,
        handlers=build_handlers(merge=merge_handler, metric_write=metric_handler),
    ).observe()

    assert merge_handler.seen == ["op-merge"]
    assert metric_handler.seen == ["op-metric"]


# --------------------------------------------------------------------------- robustness


@pytest.mark.unit
def test_a_raising_handler_does_not_abort_the_pass(journal):
    """A handler is contractually required not to raise; the dispatcher must not depend on that.

    Otherwise one defective handler hides the entire rest of the backlog.
    """
    _insert(journal, "op-1", kind="ENTITY_MERGE", created_at="2026-01-01T00:00:00+00:00")
    _insert(journal, "op-2", kind="METRIC_WRITE", created_at="2026-02-01T00:00:00+00:00")
    run = SagaReconcileDispatcher(
        journal=journal,
        handlers=build_handlers(
            merge=_Handler(raises=RuntimeError("boom")), metric_write=_Handler()
        ),
    ).observe()

    assert run.scanned == 2, "the second row must still be reached"
    by_id = {r["op_id"]: r for r in run.rows}
    assert by_id["op-1"]["outcome"] == WOULD_NEEDS_REVIEW
    assert "raised" in by_id["op-1"]["observed_error"]
    assert by_id["op-2"]["outcome"] == WOULD_REPLAY


@pytest.mark.unit
def test_observe_mutates_no_durable_state(journal):
    for i in range(5):
        _insert(journal, f"op-{i}", kind="ENTITY_MERGE")
    _insert(journal, "op-live", owner="live")
    _insert(journal, "op-legacy", owner="none")

    dispatcher = SagaReconcileDispatcher(
        journal=journal, handlers=build_handlers(merge=_Handler())
    )
    before = _dump(journal.db_path)
    dispatcher.observe()
    after = _dump(journal.db_path)

    assert after == before, "observe must not write anything, including ownership columns"


@pytest.mark.unit
def test_live_mode_is_refused_rather_than_silently_downgraded(journal):
    """Quietly observing instead would let a caller believe recovery had run."""
    dispatcher = SagaReconcileDispatcher(journal=journal, handlers={})
    with pytest.raises(NotImplementedError, match="CF-20c"):
        dispatcher.run(dry_run=False)


@pytest.mark.unit
def test_scan_is_exhaustive_beyond_the_old_500_row_horizon(journal):
    for i in range(600):
        _insert(journal, f"op-{i:04d}", kind="ENTITY_MERGE",
                created_at=f"2026-01-01T00:00:{i % 60:02d}+00:00")
    handler = _Handler()
    run = SagaReconcileDispatcher(
        journal=journal, handlers=build_handlers(merge=handler)
    ).observe()

    assert run.scanned == 600
    assert len(set(handler.seen)) == 600, "every row classified exactly once"


# --------------------------------------------------------------------------- run summary


@pytest.mark.unit
def test_run_reports_a_single_run_id_and_per_kind_counts(journal):
    _insert(journal, "op-merge", kind="ENTITY_MERGE")
    _insert(journal, "op-metric", kind="METRIC_WRITE")
    run = SagaReconcileDispatcher(
        journal=journal,
        handlers=build_handlers(merge=_Handler(), metric_write=_Handler()),
    ).observe()

    assert run.run_id and len(run.run_id) == 32
    assert run.counts_by_kind == {"ENTITY_MERGE": 1, "METRIC_WRITE": 1}
    assert run.as_dict()["dry_run"] is True


@pytest.mark.unit
def test_oldest_prepared_age_is_reported(journal):
    _insert(journal, "op-old", created_at="2026-01-01T00:00:00+00:00")
    _insert(journal, "op-new", created_at="2026-06-01T00:00:00+00:00")
    now = datetime(2026, 1, 2, tzinfo=timezone.utc)

    run = SagaReconcileDispatcher(
        journal=journal, handlers=build_handlers(merge=_Handler())
    ).observe(now=now)

    assert run.oldest_prepared_at == "2026-01-01T00:00:00+00:00"
    assert run.oldest_prepared_age_seconds == pytest.approx(86400)


@pytest.mark.unit
def test_examples_are_capped_so_a_storm_stays_readable(journal):
    for i in range(30):
        _insert(journal, f"op-{i:02d}", kind="ENTITY_MERGE")
    run = SagaReconcileDispatcher(
        journal=journal, handlers=build_handlers(merge=_Handler())
    ).observe()

    assert len(run.examples[WOULD_REPLAY]) == 5
    assert run.counts[WOULD_REPLAY] == 30, "the COUNT is still complete"


# --------------------------------------------------------------------------- readiness verdict


@pytest.mark.unit
def test_a_clean_backlog_is_write_ready(journal):
    _insert(journal, "op-1", kind="ENTITY_MERGE")
    run = SagaReconcileDispatcher(
        journal=journal, handlers=build_handlers(merge=_Handler())
    ).observe()

    assert run.write_ready is True and run.blocking_reasons == []


@pytest.mark.unit
def test_a_live_owner_alone_does_not_block_readiness(journal):
    """A live writer is normal and transient: the response is to let it finish, not refuse startup."""
    _insert(journal, "op-live", owner="live")
    run = SagaReconcileDispatcher(
        journal=journal, handlers=build_handlers(merge=_Handler())
    ).observe()

    assert run.counts[LIVE_OWNER] == 1
    assert run.write_ready is True


@pytest.mark.unit
def test_owner_unknown_blocks_readiness(journal):
    _insert(journal, "op-legacy", owner="none")
    run = SagaReconcileDispatcher(
        journal=journal, handlers=build_handlers(merge=_Handler())
    ).observe()

    assert run.write_ready is False
    assert any("unprovable ownership" in r for r in run.blocking_reasons)


@pytest.mark.unit
def test_a_quarantine_storm_is_systemic_but_a_few_rows_are_row_local(journal):
    for i in range(4):
        _insert(journal, f"op-{i}", kind="ENTITY_MERGE")
    handler = _Handler(outcome=WOULD_NEEDS_REVIEW, diagnostics={"observed_error": "drift"})

    row_local = SagaReconcileDispatcher(
        journal=journal, handlers=build_handlers(merge=handler), max_needs_review=10
    ).observe()
    assert row_local.write_ready is True, "a handful of drifted rows is row-local"

    systemic = SagaReconcileDispatcher(
        journal=journal, handlers=build_handlers(merge=handler), max_needs_review=2
    ).observe()
    assert systemic.write_ready is False
    assert any("systemic" in r for r in systemic.blocking_reasons)


@pytest.mark.unit
def test_unmapped_coordinator_reports_its_kinds_rather_than_claiming_them(journal):
    """Passing no handler for a kind must surface it, not pretend it was handled."""
    _insert(journal, "op-merge", kind="ENTITY_MERGE")
    run = SagaReconcileDispatcher(journal=journal, handlers=build_handlers()).observe()

    assert run.counts[UNKNOWN_KIND] == 1
    assert run.write_ready is False


@pytest.mark.unit
def test_skip_from_a_handler_is_still_counted(journal):
    """Defensive: a handler that disclaims a row must not vanish from the accounting."""
    _insert(journal, "op-1", kind="ENTITY_MERGE")
    run = SagaReconcileDispatcher(
        journal=journal, handlers=build_handlers(merge=_Handler(outcome=SKIP))
    ).observe()

    assert run.scanned == 1
    assert run.counts[SKIP] == 1
