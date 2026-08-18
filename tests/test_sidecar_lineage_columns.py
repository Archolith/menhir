"""Unit tests for CF-165 Phase C: subject-lineage columns on telemetry tables.

Verifies that ``mcp_events`` gains ``namespace`` / ``node_uuid`` and
``extraction_lab_runs`` gains ``namespace`` via the ``ensure_lineage_columns``
migration, and that the write paths persist and round-trip them.

All stores are built under pytest's ``tmp_path``; the real telemetry database is
never touched.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from menhir.infrastructure.telemetry.store import McpTelemetryStore

pytestmark = [pytest.mark.unit]


_OLD_MCP_EVENTS_DDL = """
CREATE TABLE mcp_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    duration_ms INTEGER NOT NULL,
    operation TEXT NOT NULL,
    kind TEXT NOT NULL,
    success INTEGER NOT NULL,
    error TEXT,
    input_size INTEGER,
    result_size INTEGER,
    payload_preview TEXT
)
"""


def _column_names(db_path: Path, table: str) -> set[str]:
    with sqlite3.connect(db_path) as conn:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _mcp_event_row(db_path: Path) -> sqlite3.Row:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            """
            SELECT namespace, node_uuid
            FROM mcp_events
            ORDER BY id ASC
            """
        ).fetchone()


def _extraction_lab_run_row(db_path: Path) -> sqlite3.Row:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            """
            SELECT namespace
            FROM extraction_lab_runs
            ORDER BY id ASC
            """
        ).fetchone()


@pytest.mark.unit
def test_fresh_store_has_lineage_columns(tmp_path: Path) -> None:
    db_path = tmp_path / "fresh.db"
    store = McpTelemetryStore(db_path=db_path)
    store._ensure_ready()

    assert {"namespace", "node_uuid"} <= _column_names(db_path, "mcp_events")
    assert "namespace" in _column_names(db_path, "extraction_lab_runs")


@pytest.mark.unit
def test_record_round_trips_namespace_and_node_uuid(tmp_path: Path) -> None:
    db_path = tmp_path / "roundtrip.db"
    store = McpTelemetryStore(db_path=db_path)
    store.record(
        kind="tool",
        operation="add_memory",
        started_at="2026-01-01T00:00:00Z",
        completed_at="2026-01-01T00:00:01Z",
        duration_ms=1000,
        success=True,
        error=None,
        input_size=10,
        result_size=20,
        payload_preview="preview",
        namespace="alpha",
        node_uuid="node-1",
    )

    row = _mcp_event_row(db_path)
    assert row is not None
    assert row["namespace"] == "alpha"
    assert row["node_uuid"] == "node-1"


@pytest.mark.unit
def test_record_without_new_kwargs_stores_null(tmp_path: Path) -> None:
    db_path = tmp_path / "no-lineage.db"
    store = McpTelemetryStore(db_path=db_path)
    store.record(
        kind="tool",
        operation="search_memory",
        started_at="2026-01-01T00:00:00Z",
        completed_at="2026-01-01T00:00:01Z",
        duration_ms=500,
        success=False,
        error="boom",
        input_size=None,
        result_size=None,
        payload_preview=None,
    )

    row = _mcp_event_row(db_path)
    assert row is not None
    assert row["namespace"] is None
    assert row["node_uuid"] is None


@pytest.mark.unit
def test_record_extraction_lab_run_round_trips_namespace(tmp_path: Path) -> None:
    db_path = tmp_path / "lab-namespace.db"
    store = McpTelemetryStore(db_path=db_path)
    run_id = store.record_extraction_lab_run(
        request_payload={"query": "q"},
        result_payload={"current_message": "hello", "arms": []},
        namespace="beta",
    )
    assert run_id is not None

    row = _extraction_lab_run_row(db_path)
    assert row is not None
    assert row["namespace"] == "beta"


@pytest.mark.unit
def test_record_extraction_lab_run_without_namespace_stores_null(tmp_path: Path) -> None:
    db_path = tmp_path / "lab-no-namespace.db"
    store = McpTelemetryStore(db_path=db_path)
    run_id = store.record_extraction_lab_run(
        request_payload={"query": "q"},
        result_payload={"current_message": "hello", "arms": []},
    )
    assert run_id is not None

    row = _extraction_lab_run_row(db_path)
    assert row is not None
    assert row["namespace"] is None


@pytest.mark.unit
def test_migration_adds_columns_preserving_old_row_and_is_idempotent(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "old.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(_OLD_MCP_EVENTS_DDL)
        conn.execute(
            """
            INSERT INTO mcp_events (
                started_at, completed_at, duration_ms, operation, kind, success,
                error, input_size, result_size, payload_preview
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "2026-01-01T00:00:00Z",
                "2026-01-01T00:00:01Z",
                100,
                "old_op",
                "tool",
                1,
                None,
                None,
                None,
                "old payload",
            ),
        )

    store = McpTelemetryStore(db_path=db_path)
    store._ensure_ready()

    assert {"namespace", "node_uuid"} <= _column_names(db_path, "mcp_events")

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT operation, namespace, node_uuid
            FROM mcp_events
            ORDER BY id ASC
            """
        ).fetchone()
    assert row is not None
    assert row["operation"] == "old_op"
    assert row["namespace"] is None
    assert row["node_uuid"] is None

    store._ensure_ready()
