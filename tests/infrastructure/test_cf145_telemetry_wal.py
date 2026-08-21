"""CF-145: WAL must be applied by the shared connection helper, not a lazy store init.

Previously ``PRAGMA journal_mode=WAL`` lived only in ``McpTelemetryStore._ensure_ready``,
which is lazy (returns early after first init). At least five stores share the telemetry
DB file, so whichever wrote first created it in rollback-journal mode. These tests assert
the shared seam ``connect_telemetry_db`` applies WAL so every writer creates the file in
WAL mode regardless of call order.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from menhir.infrastructure.pending_actions import PendingActionStore
from menhir.infrastructure.telemetry.helpers import connect_telemetry_db


def _journal_mode(db_path) -> str:
    with connect_telemetry_db(db_path) as conn:
        return conn.execute("PRAGMA journal_mode").fetchone()[0]


def test_finding_fresh_db_via_helper_is_wal(tmp_path):
    db = tmp_path / "fresh.db"
    assert _journal_mode(db) == "wal"


def test_non_telemetry_writer_gets_wal(tmp_path):
    db = tmp_path / "pending.db"
    store = PendingActionStore(db_path=db)
    store.upsert(node_uuid="cf145-node", action="compress")
    assert _journal_mode(db) == "wal"


def test_positive_control_plain_sqlite_is_delete(tmp_path):
    db = tmp_path / "control.db"
    conn = sqlite3.connect(db)
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()
    assert mode == "delete"


def test_in_memory_connection_still_usable():
    conn = connect_telemetry_db(Path(":memory:"))
    try:
        assert conn.execute("SELECT 1").fetchone()[0] == 1
    finally:
        conn.close()


def test_round_trip_write_read(tmp_path):
    db = tmp_path / "roundtrip.db"
    with connect_telemetry_db(db) as conn:
        conn.execute("CREATE TABLE t (k TEXT PRIMARY KEY, v TEXT)")
        conn.execute("INSERT INTO t (k, v) VALUES (?, ?)", ("k", "v"))
        conn.commit()
    with connect_telemetry_db(db) as conn:
        row = conn.execute("SELECT v FROM t WHERE k = ?", ("k",)).fetchone()
    assert row is not None
    assert row[0] == "v"
