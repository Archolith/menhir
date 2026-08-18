from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from menhir.infrastructure.telemetry.store import McpTelemetryStore


_OLD_MERGE_AUDIT_DDL = """
CREATE TABLE merge_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at TEXT NOT NULL,
    survivor_uuid TEXT NOT NULL,
    absorbed_uuid TEXT NOT NULL,
    similarity REAL,
    snapshot_json TEXT NOT NULL
)
"""


def _column_names(db_path: Path, table: str) -> set[str]:
    with sqlite3.connect(db_path) as conn:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


@pytest.mark.unit
def test_fresh_store_has_namespace_columns_in_merge_audit(tmp_path: Path) -> None:
    db_path = tmp_path / "fresh.db"
    store = McpTelemetryStore(db_path=db_path)
    store._ensure_ready()

    columns = _column_names(db_path, "merge_audit")
    assert "survivor_namespace" in columns
    assert "absorbed_namespace" in columns


@pytest.mark.unit
def test_record_merge_with_namespaces_round_trips(tmp_path: Path) -> None:
    db_path = tmp_path / "roundtrip.db"
    store = McpTelemetryStore(db_path=db_path)
    store.record_merge(
        survivor_uuid="surv-1",
        absorbed_uuid="absorb-1",
        similarity=0.5,
        snapshot_json="{}",
        survivor_namespace="alpha",
        absorbed_namespace="beta",
    )

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT survivor_namespace, absorbed_namespace
            FROM merge_audit
            ORDER BY id ASC
            """
        ).fetchone()
    assert row is not None
    assert row["survivor_namespace"] == "alpha"
    assert row["absorbed_namespace"] == "beta"


@pytest.mark.unit
def test_record_merge_without_namespaces_stores_null(tmp_path: Path) -> None:
    db_path = tmp_path / "no-namespace.db"
    store = McpTelemetryStore(db_path=db_path)
    store.record_merge(
        survivor_uuid="surv-2",
        absorbed_uuid="absorb-2",
        similarity=None,
        snapshot_json="{}",
    )

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT survivor_namespace, absorbed_namespace
            FROM merge_audit
            ORDER BY id ASC
            """
        ).fetchone()
    assert row is not None
    assert row["survivor_namespace"] is None
    assert row["absorbed_namespace"] is None


@pytest.mark.unit
def test_migration_adds_columns_preserving_existing_rows_and_is_idempotent(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "old.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(_OLD_MERGE_AUDIT_DDL)
        conn.execute(
            """
            INSERT INTO merge_audit (
                recorded_at, survivor_uuid, absorbed_uuid, similarity, snapshot_json
            ) VALUES (?, ?, ?, ?, ?)
            """,
            ("2026-01-01T00:00:00Z", "surv-old", "absorb-old", 0.9, '{"k": "v"}'),
        )

    store = McpTelemetryStore(db_path=db_path)
    store._ensure_ready()

    columns = _column_names(db_path, "merge_audit")
    assert "survivor_namespace" in columns
    assert "absorbed_namespace" in columns

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT survivor_uuid, absorbed_uuid, survivor_namespace, absorbed_namespace
            FROM merge_audit
            ORDER BY id ASC
            """
        ).fetchone()
    assert row is not None
    assert row["survivor_uuid"] == "surv-old"
    assert row["absorbed_uuid"] == "absorb-old"
    assert row["survivor_namespace"] is None
    assert row["absorbed_namespace"] is None

    store._ensure_ready()
