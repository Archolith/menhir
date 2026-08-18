"""Unit tests for the telemetry sidecar erasure classification registry.

Builds a real sidecar under ``tmp_path`` via ``McpTelemetryStore`` and walks the
actual schema, asserting that every TEXT column is classified as either content
(``CONTENT_COLUMNS``) or explicitly non-content (``NON_CONTENT_COLUMNS``).

This never touches the real telemetry database: it only ever uses ``tmp_path``.
"""

from __future__ import annotations

import sqlite3

import pytest

from menhir.infrastructure.telemetry.erasure_inventory import (
    CONTENT_COLUMNS,
    NON_CONTENT_COLUMNS,
    ErasureShape,
    classified_columns,
)
from menhir.infrastructure.telemetry.store import McpTelemetryStore

pytestmark = [pytest.mark.unit]


def _build_sidecar(tmp_path) -> sqlite3.Connection:
    """Create a real sidecar under tmp_path and return a read connection to it."""
    store = McpTelemetryStore(db_path=tmp_path / "t.db")
    store._ensure_ready()
    return sqlite3.connect(store.db_path)


def _text_columns(db_path) -> frozenset[tuple[str, str]]:
    """Return all (table, column) pairs of type TEXT from the real schema."""
    conn = sqlite3.connect(db_path)
    try:
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        text_cols: set[tuple[str, str]] = set()
        for table in tables:
            for info in conn.execute(f"PRAGMA table_info({table!r})"):
                _cid, name, col_type, _notnull, _dflt, _pk = info
                if col_type and col_type.upper() == "TEXT":
                    text_cols.add((table, name))
        return frozenset(text_cols)
    finally:
        conn.close()


class TestErasureInventory:
    def test_every_text_column_is_classified(self, tmp_path):
        db_path = tmp_path / "t.db"
        _build_sidecar(tmp_path)
        real = _text_columns(db_path)
        known = classified_columns() | NON_CONTENT_COLUMNS
        unclassified = sorted(real - known)
        assert not unclassified, (
            "Unclassified TEXT columns in the sidecar schema; classify each as either "
            f"a CONTENT_COLUMNS entry or a NON_CONTENT_COLUMNS entry in "
            f"src/menhir/infrastructure/telemetry/erasure_inventory.py: {unclassified}"
        )

    def test_all_content_columns_exist_in_schema(self, tmp_path):
        db_path = tmp_path / "t.db"
        _build_sidecar(tmp_path)
        conn = sqlite3.connect(db_path)
        try:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            for entry in CONTENT_COLUMNS:
                assert entry.table in tables, (
                    f"CONTENT_COLUMNS references missing table {entry.table!r}"
                )
                cols = {
                    info[1]
                    for info in conn.execute(f"PRAGMA table_info({entry.table!r})")
                }
                assert entry.column in cols, (
                    f"CONTENT_COLUMNS references missing column "
                    f"{entry.table}.{entry.column}"
                )
        finally:
            conn.close()

    def test_content_key_columns_are_valid(self, tmp_path):
        db_path = tmp_path / "t.db"
        _build_sidecar(tmp_path)
        conn = sqlite3.connect(db_path)
        try:
            for entry in CONTENT_COLUMNS:
                cols = {
                    info[1]
                    for info in conn.execute(f"PRAGMA table_info({entry.table!r})")
                }
                for key in entry.key_columns:
                    assert key in cols, (
                        f"key_columns entry {key!r} missing in "
                        f"{entry.table!r}"
                    )
                if entry.shape is ErasureShape.UNADDRESSABLE:
                    assert entry.key_columns == (), (
                        f"{entry.table}.{entry.column} is UNADDRESSABLE but declares "
                        f"key_columns {entry.key_columns}"
                    )
                else:
                    assert entry.key_columns, (
                        f"{entry.table}.{entry.column} shape {entry.shape.value} "
                        f"must declare at least one key column"
                    )
        finally:
            conn.close()

    def test_mcp_events_payload_preview_is_unaddressable(self, tmp_path):
        db_path = tmp_path / "t.db"
        _build_sidecar(tmp_path)
        match = [
            e
            for e in CONTENT_COLUMNS
            if e.table == "mcp_events" and e.column == "payload_preview"
        ]
        assert match, "mcp_events.payload_preview is not classified in CONTENT_COLUMNS"
        assert match[0].shape is ErasureShape.UNADDRESSABLE, (
            "Regression guard: mcp_events.payload_preview MUST stay UNADDRESSABLE; it "
            "carries memory text with no subject key, so a UUID-keyed purge cannot "
            "reach it (CF-167 / the CF-165 blocking schema defect)."
        )
