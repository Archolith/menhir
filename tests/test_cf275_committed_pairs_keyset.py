"""CF-275: _committed_pairs pages by keyset (created_at, rowid), not OFFSET."""

from __future__ import annotations

import json
import sqlite3

import pytest

from menhir.infrastructure import graph_operations_reads as reads
from menhir.infrastructure.graph_operations import GraphOperationsJournal


def _prep(journal, i, commit=True):
    op_id = journal.prepare(
        operation_kind="ENTITY_MERGE",
        request_json=json.dumps({"absorbed_uuid": f"a{i}", "survivor_uuid": f"s{i}"}))
    if commit:
        journal.mark_committed(op_id)
    return op_id


def _sql(path, stmt, params=()):
    conn = sqlite3.connect(path)
    try:
        conn.execute(stmt, params)
        conn.commit()
    finally:
        conn.close()


@pytest.mark.unit
def test_row_committed_behind_cursor_does_not_skip_a_later_row(tmp_path, monkeypatch):
    db = tmp_path / "ops.db"
    journal = GraphOperationsJournal(db_path=db)
    m0 = _prep(journal, 0, commit=False)
    ids = [_prep(journal, i) for i in range(1, 5)]
    _sql(db, "UPDATE graph_operations SET created_at = '1970-01-01T00:00:00' WHERE op_id = ?", (m0,))

    real = reads.connect_telemetry_db
    calls = {"n": 0}

    def wrapper(path):
        calls["n"] += 1
        if calls["n"] == 2:  # second page read: page 1 is done, commit m0 behind the cursor
            journal.mark_committed(m0)
        return real(path)

    monkeypatch.setattr(reads, "connect_telemetry_db", wrapper)
    out = journal.list_committed_merges(page_size=2)
    got = [o["op_id"] for o in out]
    for op in ids:
        assert got.count(op) == 1


@pytest.mark.unit
def test_same_created_at_ties_across_page_boundary_return_each_once_in_insertion_order(tmp_path):
    db = tmp_path / "ops.db"
    journal = GraphOperationsJournal(db_path=db)
    ids = [_prep(journal, i) for i in range(5)]
    _sql(db, "UPDATE graph_operations SET created_at = '2026-01-01T00:00:00'")
    out = journal.list_committed_merges(page_size=2)
    assert [o["op_id"] for o in out] == ids


@pytest.mark.unit
def test_malformed_last_row_of_page_still_advances_cursor(tmp_path):
    db = tmp_path / "ops.db"
    journal = GraphOperationsJournal(db_path=db)
    a = _prep(journal, 0)
    bad = _prep(journal, 1)
    c = _prep(journal, 2)
    d = _prep(journal, 3)
    _sql(db, "UPDATE graph_operations SET request_json = '{not json' WHERE op_id = ?", (bad,))
    out = journal.list_committed_merges(page_size=2)
    assert [o["op_id"] for o in out] == [a, c, d]
