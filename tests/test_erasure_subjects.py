"""Unit tests for the durable erasure-subject inventory store.

Always use pytest's ``tmp_path``; never touch the real telemetry database.
"""

from __future__ import annotations

import sqlite3

import pytest

from menhir.infrastructure.erasure_subjects import (
    SUBJECT_TYPES,
    ErasureSubjectError,
    ErasureSubjectStore,
)

SCHEMA_COLUMNS = {
    "id",
    "op_id",
    "subject_type",
    "subject_value",
    "recorded_at",
    "purged_at",
}


def _store(tmp_path) -> ErasureSubjectStore:
    return ErasureSubjectStore(db_path=tmp_path / "telemetry.db")


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


@pytest.mark.unit
def test_schema_exists_and_has_no_content_bearing_column(tmp_path):
    store = _store(tmp_path)
    store._ensure_ready()
    with sqlite3.connect(store.db_path) as conn:
        assert _columns(conn, "erasure_subjects") == SCHEMA_COLUMNS


@pytest.mark.unit
def test_record_subjects_inserts_count_and_is_idempotent(tmp_path):
    store = _store(tmp_path)
    pairs = [("NODE_UUID", "node-1"), ("NODE_UUID", "node-2"), ("NAMESPACE", "ns-1")]
    first = store.record_subjects("op-1", pairs)
    assert first == 3
    second = store.record_subjects("op-1", pairs)
    assert second == 0
    rows = store.fetch_subjects("op-1")
    assert len(rows) == 3


@pytest.mark.unit
def test_record_subjects_skips_blank_values(tmp_path):
    store = _store(tmp_path)
    inserted = store.record_subjects(
        "op-1", [("NODE_UUID", "node-1"), ("NAMESPACE", ""), ("NODE_UUID", "   ")]
    )
    assert inserted == 1
    rows = store.fetch_subjects("op-1")
    assert len(rows) == 1
    assert rows[0]["subject_value"] == "node-1"


@pytest.mark.unit
def test_record_subjects_unknown_type_raises(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(ErasureSubjectError):
        store.record_subjects("op-1", [("NOT_A_TYPE", "x")])


@pytest.mark.unit
def test_fetch_subjects_with_subject_type_filter(tmp_path):
    store = _store(tmp_path)
    store.record_subjects(
        "op-1", [("NODE_UUID", "n1"), ("NAMESPACE", "ns-1"), ("NODE_UUID", "n2")]
    )
    node_rows = store.fetch_subjects("op-1", subject_type="NODE_UUID")
    assert {r["subject_value"] for r in node_rows} == {"n1", "n2"}
    ns_rows = store.fetch_subjects("op-1", subject_type="NAMESPACE")
    assert {r["subject_value"] for r in ns_rows} == {"ns-1"}


@pytest.mark.unit
def test_fetch_subjects_unpurged_only_after_partial_purge(tmp_path):
    store = _store(tmp_path)
    store.record_subjects(
        "op-1", [("NODE_UUID", "n1"), ("NODE_UUID", "n2"), ("NODE_UUID", "n3")]
    )
    store.mark_purged("op-1", subject_type="NODE_UUID")
    store.record_subjects("op-1", [("NODE_UUID", "n4")])
    remaining = store.fetch_subjects("op-1", unpurged_only=True)
    assert {r["subject_value"] for r in remaining} == {"n4"}


@pytest.mark.unit
def test_mark_purged_stamps_only_unpurged_and_is_idempotent(tmp_path):
    store = _store(tmp_path)
    store.record_subjects("op-1", [("NODE_UUID", "n1"), ("NODE_UUID", "n2")])
    store.mark_purged("op-1", subject_type="NODE_UUID")
    rows = store.fetch_subjects("op-1")
    assert len(rows) == 2
    stamps = {r["purged_at"] for r in rows}
    assert all(r["purged_at"] is not None for r in rows)
    first_stamp = stamps.pop()
    second_count = store.mark_purged("op-1", subject_type="NODE_UUID")
    assert second_count == 0
    rows_after = store.fetch_subjects("op-1")
    assert {r["purged_at"] for r in rows_after} == {first_stamp}


@pytest.mark.unit
def test_has_live_erasure_true_then_false(tmp_path):
    store = _store(tmp_path)
    store.record_subjects("op-1", [("NODE_UUID", "n1")])
    assert store.has_live_erasure(subject_type="NODE_UUID", subject_value="n1") is True
    store.mark_purged("op-1")
    assert store.has_live_erasure(subject_type="NODE_UUID", subject_value="n1") is False


@pytest.mark.unit
def test_has_live_erasure_distinguishes_subjects(tmp_path):
    store = _store(tmp_path)
    store.record_subjects("op-1", [("NODE_UUID", "n1")])
    store.mark_purged("op-1", subject_type="NODE_UUID")

    # Re-recording a subject the same op already purged does NOT revive it. PREPARE is
    # idempotent by INSERT OR IGNORE, so a resumed erasure cannot un-purge finished work and
    # then re-suppress reads of content that is already gone.
    store.record_subjects("op-1", [("NODE_UUID", "n1")])
    assert store.has_live_erasure(subject_type="NODE_UUID", subject_value="n1") is False

    store.record_subjects("op-1", [("NODE_UUID", "n2")])
    assert store.has_live_erasure(subject_type="NODE_UUID", subject_value="n2") is True


@pytest.mark.unit
def test_transaction_enlistment_record_does_not_commit(tmp_path):
    store = _store(tmp_path)
    conn = sqlite3.connect(store.db_path)
    try:
        store.record_subjects("op-1", [("NODE_UUID", "n1")], conn=conn)
        conn.rollback()
    finally:
        conn.close()
    assert store.fetch_subjects("op-1") == []


@pytest.mark.unit
def test_count_unpurged(tmp_path):
    store = _store(tmp_path)
    store.record_subjects("op-1", [("NODE_UUID", "n1"), ("NODE_UUID", "n2")])
    store.record_subjects("op-2", [("NODE_UUID", "n3")])
    assert store.count_unpurged("op-1") == 2
    store.mark_purged("op-1", subject_type="NODE_UUID")
    assert store.count_unpurged("op-1") == 0
    assert store.count_unpurged("op-2") == 1


@pytest.mark.unit
def test_subject_types_cover_required_kinds():
    assert SUBJECT_TYPES == {"NODE_UUID", "NAMESPACE", "EPISODE_UUID", "SESSION_ID"}
