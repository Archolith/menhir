"""Only the off-loop telemetry writer may trade per-commit fsync for speed (synchronous=NORMAL).

Leases, the operations journal, receipts and recovery rows open connections through the same
`connect_telemetry_db` seam and must keep FULL: a commit they make authorizes what happens next.
"""

from __future__ import annotations

import pytest

from menhir.infrastructure.telemetry.async_writer import TELEMETRY_WRITER
from menhir.infrastructure.telemetry.helpers import connect_telemetry_db

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("real_sqlite_connect")]

_FULL, _NORMAL = 2, 1


def _synchronous_of_a_new_connection(db) -> int:
    conn = connect_telemetry_db(db)
    try:
        return int(conn.execute("PRAGMA synchronous").fetchone()[0])
    finally:
        conn.close()


def test_a_connection_off_the_writer_thread_keeps_full(tmp_path) -> None:
    assert _synchronous_of_a_new_connection(tmp_path / "t.db") == _FULL


def test_a_connection_on_the_writer_thread_uses_normal(tmp_path) -> None:
    seen: list[int] = []
    assert TELEMETRY_WRITER.submit(
        "sync-probe", lambda: seen.append(_synchronous_of_a_new_connection(tmp_path / "t.db"))
    )
    assert TELEMETRY_WRITER.drain(10)
    assert seen == [_NORMAL]


def test_a_writer_thread_connection_that_is_not_wal_keeps_full(tmp_path, monkeypatch) -> None:
    """NORMAL is only corruption-safe in WAL; a file that refuses WAL must stay FULL."""
    import menhir.infrastructure.telemetry.helpers as helpers

    db = tmp_path / "t.db"
    real_connect = helpers.sqlite3.connect

    def _connect_refusing_wal(*args, **kwargs):
        conn = real_connect(*args, **kwargs)

        def execute(sql, *a):
            if sql.strip().upper() == "PRAGMA JOURNAL_MODE=WAL":
                sql = "PRAGMA journal_mode=DELETE"
            return conn.__class__.execute(conn, sql, *a)

        conn.execute = execute  # type: ignore[method-assign]
        return conn

    monkeypatch.setattr(helpers.sqlite3, "connect", _connect_refusing_wal)
    seen: list[int] = []
    assert TELEMETRY_WRITER.submit(
        "sync-probe", lambda: seen.append(_synchronous_of_a_new_connection(db))
    )
    assert TELEMETRY_WRITER.drain(10)
    assert seen == [_FULL]
