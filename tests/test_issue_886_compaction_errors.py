"""#886: only lock contention is a retry; other OperationalErrors fail the job."""

from __future__ import annotations

import sqlite3

import pytest

from menhir.services import sidecar_compaction as sc


class _Conn:
    isolation_level = None

    def __init__(self, error: str) -> None:
        self.error = error

    def execute(self, sql, *args):
        raise sqlite3.OperationalError(self.error)

    def commit(self) -> None:
        pass

    def close(self) -> None:
        pass


def test_locked_database_is_a_retry(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(sc, "connect_telemetry_db", lambda path: _Conn("database is locked"))
    result = sc.run_sidecar_compaction(db_path=tmp_path / "t.db")
    assert result["status"] == sc.RETRY


def test_disk_full_is_not_swallowed_as_busy(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(sc, "connect_telemetry_db", lambda path: _Conn("database or disk is full"))
    with pytest.raises(sqlite3.OperationalError):
        sc.run_sidecar_compaction(db_path=tmp_path / "t.db")


class _VacuumFailsConn:
    """Migration succeeds (journal table absent); the VACUUM itself raises."""

    isolation_level = None

    def __init__(self, error: str) -> None:
        self.error = error

    def execute(self, sql, *args):
        if sql.startswith("PRAGMA table_info"):
            return []
        if sql == "VACUUM":
            raise sqlite3.OperationalError(self.error)
        raise AssertionError(sql)

    def commit(self) -> None:
        pass

    def close(self) -> None:
        pass


@pytest.mark.parametrize("message", ["database disk image is malformed", "disk I/O error"])
def test_non_busy_vacuum_error_fails_the_job_after_a_successful_migration(
    monkeypatch, tmp_path, message
) -> None:
    monkeypatch.setattr(sc, "connect_telemetry_db", lambda path: _VacuumFailsConn(message))
    with pytest.raises(sqlite3.OperationalError):
        sc.run_sidecar_compaction(db_path=tmp_path / "t.db")


def test_busy_vacuum_error_is_still_a_retry(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(sc, "connect_telemetry_db", lambda path: _VacuumFailsConn("database is locked"))
    assert sc.run_sidecar_compaction(db_path=tmp_path / "t.db")["status"] == sc.RETRY
