"""CF-20c: the preflight, and the startup barrier that decides whether writers are admitted.

Two separable things are under test.

The **preflight** answers a question about a DEPLOYMENT rather than about the code: what is in this
journal, and can this host be trusted to prove a writer dead. Its blocker/warning split is the
load-bearing part -- a blocker means recovery could not resolve the backlog, while a warning means
recovery works with a capability switched off, which is a legitimate configuration and must not
refuse activation.

The **startup barrier** decides admission. Observation must never become an outage of its own, so
its failures are swallowed; recovery is the opposite, because a deployment that asked for live
recovery and silently did not get it is running with a backlog it believes was cleared.
"""

from __future__ import annotations

import asyncio
import types

import pytest

from menhir.core import runtime as rt
from menhir.infrastructure import operation_owner as oo
from menhir.services.saga_preflight import PreflightReport, preflight_from_run


class _Run:
    """A stand-in for a completed observe() pass."""

    def __init__(self, *, write_ready=True, blocking_reasons=None, scanned=0, counts=None):
        self.run_id = "run-1"
        self.scanned = scanned
        self.counts = counts or {}
        self.counts_by_kind = {}
        self.oldest_prepared_age_seconds = None
        self.examples = {}
        self.write_ready = write_ready
        self.blocking_reasons = blocking_reasons or []
        self.aborted = False


# ------------------------------------------------------------------------------- the preflight


@pytest.mark.unit
def test_a_clean_empty_backlog_passes_preflight(monkeypatch):
    monkeypatch.setenv(oo.HOST_PID_NAMESPACE_ENV, "1")
    monkeypatch.setenv(oo.SAGA_WRITERS_GATE_AWARE_ENV, "1")

    report = preflight_from_run(_Run())

    assert report.clean is True
    assert report.blockers == []
    assert report.warnings == []


@pytest.mark.unit
def test_an_unresolvable_backlog_blocks_activation(monkeypatch):
    """A blocker means activating would leave the deployment in the fenced state it was clearing."""
    monkeypatch.setenv(oo.HOST_PID_NAMESPACE_ENV, "1")
    monkeypatch.setenv(oo.SAGA_WRITERS_GATE_AWARE_ENV, "1")

    report = preflight_from_run(
        _Run(write_ready=False, blocking_reasons=["3 PREPARED row(s) with unprovable ownership"])
    )

    assert report.clean is False
    assert "unprovable ownership" in report.blockers[0]


@pytest.mark.unit
def test_an_unasserted_pid_namespace_warns_but_does_not_block(monkeypatch):
    """The conservative configuration must not be punished.

    Recovery still runs without the assertion; it simply cannot declare a writer dead by inspecting
    a local PID, so an expired local row fences instead of replaying. Refusing to activate over
    that would push deployments toward asserting something they have not verified.
    """
    monkeypatch.delenv(oo.HOST_PID_NAMESPACE_ENV, raising=False)
    monkeypatch.setenv(oo.SAGA_WRITERS_GATE_AWARE_ENV, "1")

    report = preflight_from_run(_Run())

    assert report.clean is True, "a narrowed capability is not a blocker"
    assert report.pid_namespace_asserted is False
    assert len(report.warnings) == 1
    assert oo.HOST_PID_NAMESPACE_ENV in report.warnings[0]
    assert "containers" in report.warnings[0], "the warning must say when NOT to set it"


@pytest.mark.unit
def test_the_report_renders_its_verdict_for_a_human(monkeypatch):
    monkeypatch.delenv(oo.HOST_PID_NAMESPACE_ENV, raising=False)

    text = preflight_from_run(_Run(write_ready=False, blocking_reasons=["bad"])).render()

    assert "NOT CLEAN" in text
    assert "BLOCKER" in text
    assert "warning" in text


@pytest.mark.unit
def test_preflight_uses_the_dispatcher_it_was_given(monkeypatch):
    """A preflight that inspected different wiring would answer about a system that is not the one
    about to run."""
    monkeypatch.setenv(oo.HOST_PID_NAMESPACE_ENV, "1")
    monkeypatch.setenv(oo.SAGA_WRITERS_GATE_AWARE_ENV, "1")
    calls = []

    class _Dispatcher:
        def observe(self):
            calls.append("observe")
            return _Run()

    from menhir.services.saga_preflight import run_preflight

    report = run_preflight(_Dispatcher())

    assert calls == ["observe"], "preflight must observe, exactly once, and never replay"
    assert isinstance(report, PreflightReport)


# --------------------------------------------------------------------------- the startup barrier


def _settings(mode):
    return types.SimpleNamespace(saga_reconcile_startup_mode=mode)


def _built():
    return types.SimpleNamespace(graph_adapter=object())


@pytest.mark.unit
def test_off_mode_runs_nothing(monkeypatch):
    called = []
    monkeypatch.setattr(rt, "_recover_saga_backlog", lambda a: called.append("live"))
    monkeypatch.setattr(rt, "_observe_saga_backlog", lambda a: called.append("observe"))

    asyncio.run(rt._run_startup_saga_observe(_built(), _settings("off")))

    assert called == []


@pytest.mark.unit
def test_observe_mode_does_not_reach_the_live_path(monkeypatch):
    """The default. An upgrade must never start replaying by itself."""
    called = []
    monkeypatch.setattr(rt, "_recover_saga_backlog", lambda a: called.append("live"))
    monkeypatch.setattr(rt, "_observe_saga_backlog", lambda a: (called.append("observe"), _Run())[1])

    asyncio.run(rt._run_startup_saga_observe(_built(), _settings("observe")))

    assert called == ["observe"], "observe mode must never replay"


@pytest.mark.unit
def test_live_mode_reaches_the_recovery_path(monkeypatch):
    called = []

    def _recover(adapter):
        called.append("live")
        return _Run()

    monkeypatch.setattr(rt, "_recover_saga_backlog", _recover)

    asyncio.run(rt._run_startup_saga_observe(_built(), _settings("live")))

    assert called == ["live"]


@pytest.mark.unit
@pytest.mark.parametrize("mode", ["liv", "Live ", "replay", "on", "true", "", "yes"])
def test_a_mode_typo_can_never_arm_replay(monkeypatch, mode):
    """The failure direction that matters most in this config.

    Only the exact string "live" reaches recovery. Anything else -- a typo, a plausible synonym, a
    boolean someone assumed this was -- falls through to observation. Getting this backwards would
    mean a fat-fingered environment variable silently starts mutating a production graph, which is
    precisely the outcome the opt-in default exists to prevent.

    "Live " with a trailing space is included deliberately: the setting is lowercased and stripped
    on read, so that one SHOULD arm replay, and it is asserted separately below.
    """
    called = []
    monkeypatch.setattr(rt, "_recover_saga_backlog", lambda a: called.append("live"))
    monkeypatch.setattr(rt, "_observe_saga_backlog", lambda a: (called.append("observe"), _Run())[1])

    asyncio.run(rt._run_startup_saga_observe(_built(), _settings(mode)))

    assert "live" not in called, f"mode {mode!r} must not reach recovery"


@pytest.mark.unit
def test_the_setting_is_normalised_before_it_is_compared(monkeypatch):
    """MemorySettings lowercases and strips this value, so "  LIVE  " is a real activation.

    Asserted so the normalisation stays where it is: if it ever moved out of settings parsing, the
    typo test above would still pass while a legitimately-configured deployment silently stopped
    recovering.
    """
    from menhir.config import MemorySettings

    monkeypatch.setenv("MENHIR_SAGA_RECONCILE_STARTUP_MODE", "  LIVE  ")

    assert MemorySettings.from_env().saga_reconcile_startup_mode == "live"


@pytest.mark.unit
def test_a_peer_holding_the_gate_is_waited_out_not_treated_as_readiness(monkeypatch, tmp_path):
    """The unsound shortcut this replaced.

    Skipping recovery because a peer holds the gate looks harmless -- the peer is doing the work,
    and our PREPAREs are blocked while it holds the lease. But if that peer finds an unresolvable
    backlog, refuses its own startup and releases the gate in its ``finally``, THIS instance is
    already alive and starts admitting writes against a dirty backlog. Nothing ever told it
    recovery failed. So a held gate means wait, then reach a verdict of our own.
    """
    from menhir.services.saga_reconcile_gate import ReconciliationGate

    acquires = {"n": 0}

    def _acquire(self):
        acquires["n"] += 1
        return acquires["n"] > 2  # held by a peer for the first two attempts

    monkeypatch.setattr(ReconciliationGate, "acquire", _acquire)
    monkeypatch.setattr(ReconciliationGate, "release", lambda self: None)
    monkeypatch.setattr(rt, "_SAGA_GATE_POLL_SECONDS", 0.01)

    built = []

    class _Dispatcher:
        def observe(self):
            return _Run()

        def run(self, *, dry_run, gate):
            built.append("recovered")
            return _Run()

    import menhir.services.saga_preflight as _pf

    # _recover_saga_backlog imports build_default_dispatcher directly, so THAT is the symbol the
    # patch has to reach; patching the runtime re-export would silently do nothing.
    monkeypatch.setattr(_pf, "build_default_dispatcher", lambda a: _Dispatcher())
    monkeypatch.setenv(oo.HOST_PID_NAMESPACE_ENV, "1")
    monkeypatch.setenv(oo.SAGA_WRITERS_GATE_AWARE_ENV, "1")

    result = rt._recover_saga_backlog(object())

    assert acquires["n"] == 3, "must keep trying until the peer releases the gate"
    assert built == ["recovered"], "recovery must run HERE once the gate is obtained"
    assert result is not None


@pytest.mark.unit
def test_a_gate_never_released_refuses_startup_rather_than_admitting_writers(monkeypatch):
    """Fail closed. Never establishing a verdict is not the same as passing."""
    from menhir.services.saga_reconcile_gate import ReconciliationGate

    monkeypatch.setattr(ReconciliationGate, "acquire", lambda self: False)
    monkeypatch.setattr(ReconciliationGate, "release", lambda self: None)
    monkeypatch.setattr(rt, "_SAGA_GATE_POLL_SECONDS", 0.01)
    monkeypatch.setattr(rt, "SAGA_GATE_WAIT_SECONDS", 0.05)
    import menhir.services.saga_preflight as _pf

    monkeypatch.setattr(_pf, "build_default_dispatcher", lambda a: None)

    with pytest.raises(rt.SagaRecoveryNotWriteReady, match="never established its own"):
        rt._recover_saga_backlog(object())


@pytest.mark.unit
def test_a_verdict_of_none_refuses_startup(monkeypatch):
    """Guard for a future path that returns without establishing readiness.

    Unreachable today, and that is the point: if it ever becomes reachable, admitting writers on no
    evidence at all is the one outcome that must not happen quietly.
    """
    monkeypatch.setattr(rt, "_recover_saga_backlog", lambda a: None)

    with pytest.raises(rt.SagaRecoveryNotWriteReady, match="no verdict"):
        asyncio.run(rt._run_startup_saga_recovery(object()))


@pytest.mark.unit
def test_a_not_write_ready_recovery_refuses_to_finish_booting(monkeypatch):
    """The circuit breaker. Never "stop recovery and start normally"."""
    monkeypatch.setattr(
        rt, "_recover_saga_backlog",
        lambda a: _Run(write_ready=False, blocking_reasons=["2 rows with unprovable ownership"]),
    )

    with pytest.raises(rt.SagaRecoveryNotWriteReady, match="unprovable ownership"):
        asyncio.run(rt._run_startup_saga_recovery(object()))


@pytest.mark.unit
def test_an_observation_failure_never_breaks_boot(monkeypatch):
    """Observation exists to make a hazard visible; a bug in it must not become a new outage."""
    def _boom(adapter):
        raise RuntimeError("observation exploded")

    monkeypatch.setattr(rt, "_observe_saga_backlog", _boom)

    asyncio.run(rt._run_startup_saga_observe(_built(), _settings("observe")))  # must not raise


@pytest.mark.unit
def test_a_recovery_failure_is_fatal_unlike_an_observation_failure(monkeypatch):
    """The asymmetry, asserted directly, because it is the one thing easy to 'tidy up' later."""
    def _boom(adapter):
        raise RuntimeError("recovery exploded")

    monkeypatch.setattr(rt, "_recover_saga_backlog", _boom)

    with pytest.raises(RuntimeError, match="recovery exploded"):
        asyncio.run(rt._run_startup_saga_recovery(object()))


# ------------------------------------------------- the refusal is a process fact, not an exception


@pytest.mark.unit
def test_a_refusal_latches_this_process_out_of_every_later_initialisation(monkeypatch):
    """Raising was never enough on its own.

    ``_bootstrap_runtime_on_startup`` swallowed ordinary exceptions and ``_get_services`` cleared
    the failed init task, so a second initialisation in the SAME process could run the whole
    startup again. That second pass examines a backlog whose unresolved rows now carry this
    process's own fresh claims, which classify LIVE_OWNER and do not block -- so the startup that
    correctly refused could succeed on its next attempt with nothing having been resolved.

    So the refusal has to outlive the attempt that produced it.
    """
    monkeypatch.setattr(rt, "_saga_admission_refusal", None)
    monkeypatch.setattr(
        rt, "_recover_saga_backlog",
        lambda a: _Run(write_ready=False, blocking_reasons=["1 row whose replay failed"]),
    )

    with pytest.raises(rt.SagaRecoveryNotWriteReady, match="replay failed"):
        asyncio.run(rt._run_startup_saga_recovery(object()))

    reached = []
    monkeypatch.setattr(rt, "_run_startup_saga_observe", lambda *a: reached.append("barrier"))

    with pytest.raises(rt.SagaRecoveryNotWriteReady, match="already refused"):
        asyncio.run(rt._initialize_services(_settings("live")))

    assert reached == [], "a latched process must refuse before it rebuilds anything"


@pytest.mark.unit
def test_the_refusal_names_the_condition_that_actually_broke_readiness(monkeypatch):
    """The first reason is kept; a later one is a consequence of already being latched."""
    monkeypatch.setattr(rt, "_saga_admission_refusal", None)

    rt._refuse_saga_write_admission("the original blocker")
    rt._refuse_saga_write_admission("a later consequence")

    assert rt._saga_write_admission_refused() == "the original blocker"


@pytest.mark.unit
def test_mcp_lifespan_never_yields_a_service_after_a_refusal(monkeypatch):
    """"Refusing startup" must not mean "logging that startup failed while staying available".

    The bootstrap is backgrounded so stdio startup is not blocked by a slow init, which is right
    for observe mode where the pass is advisory. In live mode the bootstrap IS the write-readiness
    barrier, so yielding before it resolves serves a process that never established one.
    """
    monkeypatch.setattr(rt, "_saga_admission_refusal", None)
    monkeypatch.setattr(rt, "_saga_recovery_is_armed", lambda: True)

    async def _refuse():
        raise rt.SagaRecoveryNotWriteReady("recovery refused")

    monkeypatch.setattr(rt, "_bootstrap_runtime_on_startup", _refuse)

    async def _drive():
        yielded = False
        with pytest.raises(rt.SagaRecoveryNotWriteReady):
            async with rt.mcp_lifespan(object()):
                yielded = True
        return yielded

    assert asyncio.run(_drive()) is False, "the lifespan must not yield after a refusal"


@pytest.mark.unit
def test_observe_mode_still_starts_without_waiting_for_the_bootstrap(monkeypatch):
    """The behaviour the live barrier must not cost everyone else.

    Observe mode has no readiness verdict to wait for, so its bootstrap stays in the background
    and startup is not held behind a full init.
    """
    monkeypatch.setattr(rt, "_saga_admission_refusal", None)
    monkeypatch.setattr(rt, "_saga_recovery_is_armed", lambda: False)

    started = asyncio.Event()

    async def _slow():
        started.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(rt, "_bootstrap_runtime_on_startup", _slow)

    async def _drive():
        async with rt.mcp_lifespan(object()):
            return started.is_set() or True

    assert asyncio.run(_drive()) is True


@pytest.mark.unit
def test_an_unreadable_mode_waits_rather_than_guessing(monkeypatch):
    """Fail closed: not knowing whether recovery was armed must not mean serving as if it was not."""
    def _boom():
        raise RuntimeError("settings unreadable")

    monkeypatch.setattr(rt.MemorySettings, "from_env", staticmethod(_boom))

    assert rt._saga_recovery_is_armed() is True


# ------------------------------------------------------- the real gate lifecycle, unmocked


@pytest.mark.unit
def test_a_dirty_backlog_releases_the_gate_before_refusing_to_boot(monkeypatch, tmp_path):
    """The path every other test in this file mocks away.

    Refusing to boot is what keeps writers out of THIS instance. Keeping the SQLite lease as well
    would add nothing here while blocking healthy peers and the operator tooling needed to fix the
    problem, and it would outlive the process by its whole TTL. So the gate must come back even on
    the failure path.
    """
    import sqlite3

    from menhir.infrastructure.graph_operations import (
        RECONCILIATION_LEASE_NAME,
        GraphOperationsJournal,
    )
    from menhir.services.scheduler_lease import SchedulerLeaseStore

    monkeypatch.setenv(oo.HOST_PID_NAMESPACE_ENV, "1")

    # A kind no reconciler claims: unresolvable, so the preflight cannot be clean.
    journal = GraphOperationsJournal()
    journal._ensure_ready()
    with sqlite3.connect(journal.db_path) as conn:
        conn.execute(
            "INSERT INTO graph_operations (op_id, operation_kind, request_json, state, "
            "created_at, updated_at) "
            "VALUES ('op-weird', 'METRIC_MIGRATE', '{}', 'PREPARED', ?, ?)",
            ("2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
        )
        conn.commit()

    with pytest.raises(rt.SagaRecoveryNotWriteReady, match="NOT clean"):
        rt._recover_saga_backlog(object())

    holder = SchedulerLeaseStore().fetch(lease_name=RECONCILIATION_LEASE_NAME)
    assert holder is None or holder["owner_id"] is None or holder.get("expired"), (
        f"the reconciliation gate must be released even when recovery refuses: {holder}"
    )
    assert journal.get("op-weird")["state"] == "PREPARED", (
        "a refused recovery must not have touched the backlog"
    )


# ------------------------------------------------- mixed-version writers are out of scope, stated


@pytest.mark.unit
def test_unquiesced_writers_block_activation(monkeypatch):
    """The one remaining High, converted from a silent assumption into a stated precondition.

    The PREPARE pause is enforced inside prepare(), so it binds only writers running a gate-aware
    build. An older binary can still insert a PREPARED row while recovery holds the gate, begin
    mutating, and be missed by the pass that then declares write-ready. The new binary cannot
    prevent that -- the bypassing process is the one without the check -- so live recovery declares
    mixed-version writers out of scope and refuses until an operator says none can be running.
    """
    monkeypatch.setenv(oo.HOST_PID_NAMESPACE_ENV, "1")
    monkeypatch.delenv(oo.SAGA_WRITERS_GATE_AWARE_ENV, raising=False)

    report = preflight_from_run(_Run())

    assert report.clean is False
    assert report.writers_gate_aware is False
    assert any(oo.SAGA_WRITERS_GATE_AWARE_ENV in b for b in report.blockers)


@pytest.mark.unit
def test_unquiesced_writers_are_a_blocker_not_a_warning(monkeypatch):
    """The distinction from the PID-namespace assertion, asserted directly.

    An unasserted PID namespace NARROWS recovery: it can no longer prove a local writer died, so
    rows fence. That is safe and merely costs automatic recovery, hence a warning. An unasserted
    writer quiesce ADMITS a writer racing recovery, which is unsafe, hence a blocker. Collapsing
    the two would make one of them wrong.
    """
    monkeypatch.delenv(oo.HOST_PID_NAMESPACE_ENV, raising=False)
    monkeypatch.delenv(oo.SAGA_WRITERS_GATE_AWARE_ENV, raising=False)

    report = preflight_from_run(_Run())

    assert len(report.warnings) == 1, "the PID namespace stays a warning"
    assert oo.HOST_PID_NAMESPACE_ENV in report.warnings[0]
    assert len(report.blockers) == 1, "the writer quiesce is a blocker"
    assert oo.SAGA_WRITERS_GATE_AWARE_ENV in report.blockers[0]
    assert report.clean is False


@pytest.mark.unit
def test_live_startup_refuses_when_writers_are_not_quiesced(monkeypatch):
    """End to end: the assertion actually gates arming, not just the report."""
    from menhir.services.saga_reconcile_gate import ReconciliationGate
    import menhir.services.saga_preflight as _pf

    monkeypatch.setenv(oo.HOST_PID_NAMESPACE_ENV, "1")
    monkeypatch.delenv(oo.SAGA_WRITERS_GATE_AWARE_ENV, raising=False)
    monkeypatch.setattr(ReconciliationGate, "acquire", lambda self: True)
    monkeypatch.setattr(ReconciliationGate, "release", lambda self: None)

    class _Dispatcher:
        def observe(self):
            return _Run()

        def run(self, *, dry_run, gate):  # pragma: no cover -- must never be reached
            raise AssertionError("recovery must not replay with unquiesced writers")

    monkeypatch.setattr(_pf, "build_default_dispatcher", lambda a: _Dispatcher())

    with pytest.raises(rt.SagaRecoveryNotWriteReady, match="NOT clean"):
        rt._recover_saga_backlog(object())


@pytest.mark.unit
@pytest.mark.parametrize("value", ["", "0", "false", "no", "soon", "partly"])
def test_only_an_affirmative_quiesce_counts(monkeypatch, value):
    """A hedge is not an assertion."""
    monkeypatch.setenv(oo.SAGA_WRITERS_GATE_AWARE_ENV, value)
    assert oo.all_saga_writers_are_gate_aware() is False
