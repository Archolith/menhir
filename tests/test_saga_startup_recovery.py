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

    report = preflight_from_run(_Run())

    assert report.clean is True
    assert report.blockers == []
    assert report.warnings == []


@pytest.mark.unit
def test_an_unresolvable_backlog_blocks_activation(monkeypatch):
    """A blocker means activating would leave the deployment in the fenced state it was clearing."""
    monkeypatch.setenv(oo.HOST_PID_NAMESPACE_ENV, "1")

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
def test_a_gate_held_elsewhere_skips_recovery_without_failing_startup(monkeypatch):
    """Another instance is doing the work. Contending would be worse than skipping."""
    monkeypatch.setattr(rt, "_recover_saga_backlog", lambda a: None)

    asyncio.run(rt._run_startup_saga_recovery(object()))  # must not raise


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
