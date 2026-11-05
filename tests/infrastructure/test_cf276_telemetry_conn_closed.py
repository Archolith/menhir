"""CF-276: connect_telemetry_db's `with` block closes the connection."""

from __future__ import annotations

import sqlite3

import pytest

from menhir.infrastructure.telemetry.helpers import connect_telemetry_db


@pytest.mark.unit
def test_with_block_closes_connection(tmp_path):
    with connect_telemetry_db(tmp_path / "t.db") as c:
        c.execute("SELECT 1")
    with pytest.raises(sqlite3.ProgrammingError):
        c.execute("SELECT 1")


@pytest.mark.unit
def test_with_block_still_commits_before_close(tmp_path):
    p = tmp_path / "t.db"
    with connect_telemetry_db(p) as c:
        c.execute("CREATE TABLE t (x INTEGER)")
        c.execute("INSERT INTO t VALUES (1)")
    other = sqlite3.connect(p)
    try:
        assert other.execute("SELECT x FROM t").fetchall() == [(1,)]
    finally:
        other.close()


@pytest.mark.unit
def test_with_block_rolls_back_and_closes_on_exception(tmp_path):
    p = tmp_path / "t.db"
    with connect_telemetry_db(p) as c0:
        c0.execute("CREATE TABLE t (x INTEGER)")
    with pytest.raises(RuntimeError), connect_telemetry_db(p) as c:
        c.execute("INSERT INTO t VALUES (1)")
        raise RuntimeError("boom")
    with pytest.raises(sqlite3.ProgrammingError):
        c.execute("SELECT 1")
    other = sqlite3.connect(p)
    try:
        assert other.execute("SELECT COUNT(*) FROM t").fetchone() == (0,)
    finally:
        other.close()


@pytest.mark.unit
def test_bare_connection_without_with_stays_open_until_closed(tmp_path):
    c = connect_telemetry_db(tmp_path / "t.db")
    c.execute("SELECT 1")
    c.execute("SELECT 1")
    c.close()
    with pytest.raises(sqlite3.ProgrammingError):
        c.execute("SELECT 1")
