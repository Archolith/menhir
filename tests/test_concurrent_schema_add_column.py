"""Two stores initializing one SQLite file at once must not fail with "duplicate column name".

Each migration reads the table's columns, then adds the missing ones. Two stores that both read
before either adds see the same column missing; the second ALTER then raised. CI hit it as a
failed delete_memory (a fresh McpTelemetryStore racing another on the same file).
"""

from __future__ import annotations

import sqlite3

import pytest

from menhir.infrastructure.telemetry.llm_usage_store import initialize_llm_usage_schema

pytestmark = pytest.mark.unit


def test_a_second_add_after_a_stale_column_read_is_success(tmp_path) -> None:
    """The interleaving itself: both stores read the column as missing, then both add it."""
    from menhir.infrastructure.telemetry.helpers import add_column_if_missing

    conn = sqlite3.connect(tmp_path / "t.db")
    try:
        conn.execute("CREATE TABLE t (id INTEGER)")
        stale = {"id"}
        add_column_if_missing(conn, "t", "c", "TEXT", stale)
        add_column_if_missing(conn, "t", "c", "TEXT", stale)  # raised before the fix
        assert [r[1] for r in conn.execute("PRAGMA table_info(t)")] == ["id", "c"]
    finally:
        conn.close()


def test_other_alter_errors_still_raise(tmp_path) -> None:
    from menhir.infrastructure.telemetry.helpers import add_column_if_missing

    conn = sqlite3.connect(tmp_path / "t.db")
    try:
        with pytest.raises(sqlite3.OperationalError, match="no such table"):
            add_column_if_missing(conn, "missing_table", "c", "TEXT", set())
    finally:
        conn.close()


def test_llm_usage_migration_survives_a_peer_that_added_the_columns_first(tmp_path) -> None:
    """An old-schema table, migrated by a peer between this store's column read and its ALTERs."""
    db = tmp_path / "t.db"
    conn = sqlite3.connect(db, isolation_level=None)
    # A sidecar from before the three migrated columns existed: today's schema minus them.
    initialize_llm_usage_schema(conn)
    for column in ("error_code", "cache_write_input_tokens", "service_tier"):
        conn.execute(f"ALTER TABLE llm_usage_events DROP COLUMN {column}")
    real_execute = conn.execute

    class _PeerMigratesAfterTheRead:
        """Delegates to the real connection, but lets a peer migrate right after the PRAGMA."""

        def execute(self, sql, *args):
            result = real_execute(sql, *args)
            if sql.startswith("PRAGMA table_info(llm_usage_events)"):
                rows = result.fetchall()
                peer = sqlite3.connect(db, isolation_level=None)
                try:
                    initialize_llm_usage_schema(peer)
                finally:
                    peer.close()
                return iter(rows)
            return result

    try:
        initialize_llm_usage_schema(_PeerMigratesAfterTheRead())  # raised before the fix
    finally:
        conn.close()

