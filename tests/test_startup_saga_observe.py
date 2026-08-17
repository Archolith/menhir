"""CF-20b: the startup saga-observation pass.

Every test here isolates the sidecar via MENHIR_MCP_TELEMETRY_DB. That is not incidental tidiness:
``_observe_saga_backlog`` constructs ``GraphOperationsJournal()`` with no path, exactly as the three
production call sites do, so without the override these tests would open the operator's real
telemetry database.
"""

from __future__ import annotations

import logging
import sqlite3

import pytest

from menhir.core import runtime


@pytest.fixture()
def isolated_sidecar(tmp_path, monkeypatch):
    db = tmp_path / "ops.db"
    monkeypatch.setenv("MENHIR_MCP_TELEMETRY_DB", str(db))
    return db


class _Settings:
    def __init__(self, mode="observe"):
        self.saga_reconcile_startup_mode = mode


class _Built:
    def __init__(self, adapter=None):
        self.graph_adapter = adapter


class _Run:
    def __init__(self, *, scanned=1, write_ready=True, reasons=None):
        self.run_id = "deadbeef"
        self.scanned = scanned
        self.counts = {"WOULD_REPLAY": scanned}
        self.counts_by_kind = {"ENTITY_MERGE": scanned}
        self.examples = {"WOULD_REPLAY": ["op-1"]}
        self.oldest_prepared_age_seconds = 42.0
        self.write_ready = write_ready
        self.blocking_reasons = reasons or []


# --------------------------------------------------------------------------- gating


async def test_off_mode_does_not_observe_at_all(monkeypatch):
    called = []
    monkeypatch.setattr(runtime, "_observe_saga_backlog", lambda a: called.append(a))

    await runtime._run_startup_saga_observe(_Built(adapter=object()), _Settings(mode="off"))

    assert called == [], "off must skip the pass entirely, not observe and discard"


async def test_missing_graph_adapter_is_a_no_op(monkeypatch):
    called = []
    monkeypatch.setattr(runtime, "_observe_saga_backlog", lambda a: called.append(a))

    await runtime._run_startup_saga_observe(_Built(adapter=None), _Settings())

    assert called == []


async def test_observe_mode_is_the_default_when_the_setting_is_absent(monkeypatch):
    """A settings object predating this flag must still get the pass, not silently skip it."""
    called = []
    monkeypatch.setattr(runtime, "_observe_saga_backlog", lambda a: called.append(a) or _Run())

    await runtime._run_startup_saga_observe(_Built(adapter=object()), object())

    assert len(called) == 1


# --------------------------------------------------------------------------- failure containment


async def test_a_failing_observation_never_breaks_boot(monkeypatch, caplog):
    """The pass exists to make a latent hazard visible; a bug in it must not become an outage."""
    def _boom(_adapter):
        raise RuntimeError("observation exploded")

    monkeypatch.setattr(runtime, "_observe_saga_backlog", _boom)

    with caplog.at_level(logging.WARNING):
        await runtime._run_startup_saga_observe(_Built(adapter=object()), _Settings())

    assert any("saga reconcile observation failed" in r.message.lower() for r in caplog.records)


# --------------------------------------------------------------------------- reporting


async def test_a_clean_empty_backlog_does_not_log_an_alarming_summary(monkeypatch, caplog):
    monkeypatch.setattr(runtime, "_observe_saga_backlog", lambda a: _Run(scanned=0))

    with caplog.at_level(logging.INFO):
        await runtime._run_startup_saga_observe(_Built(adapter=object()), _Settings())

    assert not [r for r in caplog.records if r.levelno >= logging.INFO], (
        "the common zero-PREPARED startup must stay quiet"
    )


async def test_a_backlog_is_summarised_at_info(monkeypatch, caplog):
    monkeypatch.setattr(runtime, "_observe_saga_backlog", lambda a: _Run(scanned=3))

    with caplog.at_level(logging.INFO):
        await runtime._run_startup_saga_observe(_Built(adapter=object()), _Settings())

    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "deadbeef" in messages, "the run id must be reported so one event reads as one incident"
    assert "scanned=3" in messages


async def test_not_write_ready_warns_but_states_nothing_is_blocked(monkeypatch, caplog):
    """The verdict is advisory in CF-20b. The log must not imply recovery ran or writes stopped."""
    monkeypatch.setattr(
        runtime,
        "_observe_saga_backlog",
        lambda a: _Run(scanned=2, write_ready=False, reasons=["2 row(s) with unprovable ownership"]),
    )

    with caplog.at_level(logging.WARNING):
        await runtime._run_startup_saga_observe(_Built(adapter=object()), _Settings())

    warnings = " ".join(
        r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING
    )
    assert "unprovable ownership" in warnings
    assert "Nothing is blocked" in warnings


# --------------------------------------------------------------------------- real assembly


def test_observe_backlog_assembles_over_the_isolated_sidecar(isolated_sidecar):
    """The real helper, against a throwaway DB.

    With zero PREPARED rows no handler is ever consulted, which is why a bare object() suffices as
    the graph adapter -- and is exactly why the common startup costs one indexed read.
    """
    run = runtime._observe_saga_backlog(object())

    assert run.scanned == 0
    assert run.write_ready is True
    assert isolated_sidecar.exists(), "the journal must have been created on the override path"


def test_observe_backlog_classifies_a_prepared_row_without_mutating_it(isolated_sidecar):
    from menhir.infrastructure.graph_operations import GraphOperationsJournal

    journal = GraphOperationsJournal(db_path=isolated_sidecar)
    journal._ensure_ready()
    with sqlite3.connect(isolated_sidecar) as conn:
        conn.execute(
            "INSERT INTO graph_operations (op_id, operation_kind, request_json, state, "
            "created_at, updated_at) VALUES ('legacy-1', 'ENTITY_MERGE', '{}', 'PREPARED', "
            "'2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
        )
        conn.commit()

    before = [tuple(r) for r in sqlite3.connect(isolated_sidecar).execute(
        "SELECT * FROM graph_operations ORDER BY op_id")]
    run = runtime._observe_saga_backlog(object())
    after = [tuple(r) for r in sqlite3.connect(isolated_sidecar).execute(
        "SELECT * FROM graph_operations ORDER BY op_id")]

    assert run.scanned == 1
    # Ownerless legacy row: unprovable liveness, so it must not be treated as recoverable.
    assert run.counts["OWNER_UNKNOWN"] == 1
    assert run.write_ready is False
    assert after == before, "the startup pass must not mutate the journal"
