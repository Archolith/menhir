"""CF-171: the telemetry sidecar had no retention on any high-volume table.

Owner decision 2026-08-19: tiered by role -- ~30d observability, ~90d diagnostic, and
`merge_audit` never time-pruned because it is recovery material, not observability.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from menhir.infrastructure.telemetry.lifecycle_store import TelemetryLifecycleStoreMixin
from menhir.infrastructure.telemetry.store import McpTelemetryStore

pytestmark = pytest.mark.unit


@pytest.fixture
def store(tmp_path: Path) -> McpTelemetryStore:
    st = McpTelemetryStore(db_path=tmp_path / "telemetry.db")
    st._ensure_ready()
    return st


def _rows(store: McpTelemetryStore, table: str) -> int:
    with sqlite3.connect(store.db_path) as conn:
        return int(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])


def _seed(store: McpTelemetryStore, sql: str, *params) -> None:
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(sql, params)
        conn.commit()


def test_cf171_old_observability_rows_are_pruned(store: McpTelemetryStore) -> None:
    """`lifecycle_events` writes ~30 rows per ingest at ~664 bytes/row -- the measured
    ~1.9 GiB per 100k ingests, in the file six writers contend on."""
    _seed(
        store,
        "INSERT INTO lifecycle_events(recorded_at,phase,event,status) VALUES(?,?,?,?)",
        "2020-01-01T00:00:00", "p", "e", "s",
    )
    _seed(
        store,
        "INSERT INTO lifecycle_events(recorded_at,phase,event,status) "
        "VALUES(datetime('now'),?,?,?)",
        "p", "e", "s",
    )

    deleted = store.prune_telemetry_tables(observability_days=30, diagnostic_days=90)

    assert deleted["lifecycle_events"] == 1
    assert _rows(store, "lifecycle_events") == 1


def test_cf171_the_two_tiers_have_independent_windows(store: McpTelemetryStore) -> None:
    """A row 60 days old is past the observability window and inside the diagnostic one. A single
    uniform window would delete the failure history someone needs to correlate a defect that
    recurs quarterly, which is why the tiers are split by role rather than by convenience."""
    for table, cols, ts in (
        ("lifecycle_events", "(recorded_at,phase,event,status)", ("p", "e", "s")),
        ("failure_events",
         "(recorded_at,operation,failure_stage,classification,retryable,error)",
         ("o", "s", "c", 0, "e")),
    ):
        placeholders = ",".join("?" * len(ts))
        _seed(
            store,
            f"INSERT INTO {table}{cols} VALUES(datetime('now','-60 days'),{placeholders})",
            *ts,
        )

    deleted = store.prune_telemetry_tables(observability_days=30, diagnostic_days=90)

    assert deleted.get("lifecycle_events") == 1, "60d row survived the 30d observability window"
    assert "failure_events" not in deleted, "60d row was deleted inside the 90d diagnostic window"
    assert _rows(store, "failure_events") == 1


def test_cf171_t_separated_timestamps_compare_correctly(store: McpTelemetryStore) -> None:
    """The CF-6 trap. ISO-8601 values carry a 'T' and `datetime('now', ...)` uses a space, so a
    plain `<` compares wrong -- which would make this pruner silently delete nothing, or delete
    the wrong side of the boundary. Both rows here are written in the 'T' form on purpose."""
    _seed(
        store,
        "INSERT INTO lifecycle_events(recorded_at,phase,event,status) VALUES(?,?,?,?)",
        "2020-01-01T00:00:00", "p", "e", "s",
    )
    _seed(
        store,
        "INSERT INTO lifecycle_events(recorded_at,phase,event,status) VALUES(?,?,?,?)",
        "2099-01-01T00:00:00", "p", "e", "s",
    )

    store.prune_telemetry_tables(observability_days=30, diagnostic_days=90)

    with sqlite3.connect(store.db_path) as conn:
        remaining = [r[0] for r in conn.execute("SELECT recorded_at FROM lifecycle_events")]
    assert remaining == ["2099-01-01T00:00:00"]


def test_cf171_merge_audit_is_never_time_pruned(store: McpTelemetryStore) -> None:
    """The one table that is NOT observability. `snapshot_json` holds the absorbed node's content
    and is the only surviving copy once a merge DETACH-DELETEs that node;
    `legacy_unmerge_coordinator` reads it to restore, and `merge_recoverability` reads it to
    report whether a merge can be undone at all. The row outliving the node is the design.

    Time-pruning it would convert "recoverable" into "permanently lost" -- data destruction
    dressed as disk hygiene. Erasure already suppresses these rows by subject, which is the
    correct deletion axis.
    """
    assert "merge_audit" not in TelemetryLifecycleStoreMixin._RETENTION_TIERS

    # Namespaced so the CF-165 drop-unowned trigger does not remove it on insert.
    _seed(
        store,
        "INSERT INTO merge_audit"
        "(recorded_at,survivor_uuid,absorbed_uuid,snapshot_json,"
        " survivor_namespace,absorbed_namespace) VALUES(?,?,?,?,?,?)",
        "2019-01-01T00:00:00", "s", "a", "{}", "ns", "ns",
    )
    assert _rows(store, "merge_audit") == 1

    store.prune_telemetry_tables(observability_days=1, diagnostic_days=1)

    assert _rows(store, "merge_audit") == 1, "recovery material was time-pruned"


def test_cf171_a_missing_table_does_not_abort_the_sweep(store: McpTelemetryStore) -> None:
    """Four of the nine are created lazily or live in a separate store file. A pruner that raised
    on a missing table would abort mid-sweep and leave the high-volume tables unpruned -- the
    failure would present as "retention isn't working" with no error anyone sees."""
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("DROP TABLE IF EXISTS recall_lab_runs")
        conn.commit()
    _seed(
        store,
        "INSERT INTO lifecycle_events(recorded_at,phase,event,status) VALUES(?,?,?,?)",
        "2020-01-01T00:00:00", "p", "e", "s",
    )

    deleted = store.prune_telemetry_tables(observability_days=30, diagnostic_days=90)

    assert deleted.get("lifecycle_events") == 1
    assert "recall_lab_runs" not in deleted


def test_cf171_recent_rows_are_never_deleted(store: McpTelemetryStore) -> None:
    for _ in range(3):
        _seed(
            store,
            "INSERT INTO lifecycle_events(recorded_at,phase,event,status) "
            "VALUES(datetime('now'),?,?,?)",
            "p", "e", "s",
        )
    assert store.prune_telemetry_tables(observability_days=30, diagnostic_days=90) == {}
    assert _rows(store, "lifecycle_events") == 3


@pytest.mark.asyncio
async def test_cf171_both_tiers_disabled_skips_entirely() -> None:
    """0 disables a tier. With both off the job must not touch the database at all, rather than
    quietly pruning at some default."""
    from menhir.services.scheduler_tasks import prune_telemetry_tables

    result = await prune_telemetry_tables(observability_days=0, diagnostic_days=0)
    assert result == {"pruned": {}, "skipped": "both tiers disabled"}


def test_cf171_the_job_is_registered_and_the_settings_are_threaded() -> None:
    """CF-166 is the sibling: a retention control that was written, tested, and never called,
    while the runbook stated it was enforced. Leaving these settings parsed but unread would
    reproduce that exactly, so both halves are pinned here."""
    import inspect

    from menhir.core import runtime
    from menhir.services.maintenance_scheduler import MaintenanceScheduler

    scheduler = MaintenanceScheduler(
        ingest_service=object(),
        graph_adapter=object(),
        telemetry_observability_retention_days=30,
        telemetry_diagnostic_retention_days=90,
    )
    assert "prune_telemetry_tables" in scheduler._jobs

    disabled = MaintenanceScheduler(
        ingest_service=object(),
        graph_adapter=object(),
        telemetry_observability_retention_days=0,
        telemetry_diagnostic_retention_days=0,
    )
    assert "prune_telemetry_tables" not in disabled._jobs

    source = inspect.getsource(runtime)
    assert "telemetry_observability_retention_days=getattr(" in source
    assert "telemetry_diagnostic_retention_days=getattr(" in source
