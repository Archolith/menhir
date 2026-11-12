"""P3 of the erasure rebuild: sidecar physical erasure (real SQLite under tmp_path, no Neo4j).

Covers the secure connection seam, the erased-subject denylist and its generated triggers (with
the down-migration), the op-keyed subquery purge, FLAGGED_UUID / CONTEXT_HOLDER_UUID column
purges, ``suppressed_node_uuids`` and the scheduled compaction job, including a raw-bytes proof.
"""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

import pytest

from menhir.domain.erasure import ERASED_MARKER
from menhir.infrastructure.erasure_subjects import ErasureSubjectStore, suppressed_node_uuids
from menhir.infrastructure.graph_operations import GraphOperationsJournal
from menhir.infrastructure.telemetry import helpers
from menhir.infrastructure.telemetry.erasure_denylist import (
    TRIGGER_PREFIX,
    denylist_plan,
    drop_erasure_denylist_triggers,
    ensure_erasure_denylist,
)
from menhir.infrastructure.telemetry.erasure_inventory import (
    CONTENT_COLUMNS,
    DENYLIST_EXCLUDED,
)
from menhir.infrastructure.telemetry.erasure_purge import (
    ErasureSubjects,
    count_residual_content,
    purge_content,
)
from menhir.infrastructure.telemetry.helpers import connect_telemetry_db
from menhir.infrastructure.telemetry.store import McpTelemetryStore
from menhir.services.erasure_coordinator import ErasureCoordinator
from menhir.services.sidecar_compaction import COMPACTED, RETRY, run_sidecar_compaction

pytestmark = [pytest.mark.unit]


def _id() -> str:
    return str(uuid.uuid4())


class _Adapter:
    def episodic_twin_uuids(self, node_uuid):
        return []

    def delete_memory_detailed(self, node_uuid, **_kw):
        return {"touched": True}

    def delete_memory(self, node_uuid):
        return True

    def capture_namespace_uuids(self, group_id, namespace=None):
        return []

    def delete_namespace(self, group_id, namespace=None):
        return 1


def _db(tmp_path) -> Path:
    db = tmp_path / "t.db"
    McpTelemetryStore(db_path=db)._ensure_ready()
    GraphOperationsJournal(db_path=db)._ensure_ready()
    ErasureSubjectStore(db_path=db)._ensure_ready()
    return db


def _failure(db: Path, episode_uuid: str | None, error: str = "boom", details: str | None = None):
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO failure_events (recorded_at, operation, episode_uuid, error, details_json) "
            "VALUES ('2026-01-01T00:00:00+00:00', 'enrich', ?, ?, ?)",
            (episode_uuid, error, details),
        )


def _failure_rows(db: Path, episode_uuid: str) -> list[tuple]:
    with sqlite3.connect(db) as conn:
        return conn.execute(
            "SELECT error, details_json FROM failure_events WHERE episode_uuid = ?",
            (episode_uuid,),
        ).fetchall()


def _prepare_erasure_op(db: Path, subjects: list[tuple[str, str]], kind="EXPLICIT_ERASURE") -> str:
    op_id = uuid.uuid4().hex
    journal = GraphOperationsJournal(db_path=db)
    with connect_telemetry_db(db) as conn:
        journal.prepare(operation_kind=kind, request_json="{}", op_id=op_id, conn=conn)
        ErasureSubjectStore(db_path=db).record_subjects(op_id, subjects, conn=conn)
        conn.commit()
    return op_id


def _denylist(db: Path) -> set[str]:
    with sqlite3.connect(db) as conn:
        return {r[0] for r in conn.execute("SELECT subject_value FROM erased_subject_denylist")}


# ------------------------------------------------------------------ 1. connection seam


def test_connect_telemetry_db_sets_secure_delete_and_memory_temp_store(tmp_path):
    with connect_telemetry_db(tmp_path / "x.db") as conn:
        assert conn.execute("PRAGMA secure_delete").fetchone()[0] == 1
        assert conn.execute("PRAGMA temp_store").fetchone()[0] == 2  # MEMORY


def test_coordinator_and_subject_store_connections_go_through_the_seam(tmp_path):
    db = _db(tmp_path)
    coord = ErasureCoordinator(
        graph_adapter=_Adapter(),
        journal=GraphOperationsJournal(db_path=db),
        subjects=ErasureSubjectStore(db_path=db),
    )
    with coord._connect() as conn:
        assert conn.execute("PRAGMA secure_delete").fetchone()[0] == 1
        assert conn.execute("PRAGMA temp_store").fetchone()[0] == 2


# ------------------------------------------------------------------ 2. denylist and triggers


def test_denylist_filled_with_closure_subjects_only_for_explicit_erasure(tmp_path):
    db = _db(tmp_path)
    node, derived, ns, flagged, holder = _id(), _id(), "ns-x", _id(), _id()
    _prepare_erasure_op(
        db,
        [("NODE_UUID", node), ("DERIVED_UUID", derived), ("NAMESPACE", ns),
         ("FLAGGED_UUID", flagged), ("CONTEXT_HOLDER_UUID", holder)],
    )
    assert _denylist(db) == {node, derived}

    other = _id()
    _prepare_erasure_op(db, [("NODE_UUID", other)], kind="ENTITY_DELETE")
    assert other not in _denylist(db)


def test_triggers_drop_late_insert_but_not_other_subjects(tmp_path):
    db = _db(tmp_path)
    erased, live = _id(), _id()
    _prepare_erasure_op(db, [("NODE_UUID", erased)])
    _failure(db, erased, "late")
    _failure(db, live, "kept")
    assert _failure_rows(db, erased) == []
    assert _failure_rows(db, live) == [("kept", None)]


def test_update_trigger_blocks_content_writes_but_allows_blanking(tmp_path):
    db = _db(tmp_path)
    erased = _id()
    _failure(db, erased, "orig", '{"a": 1}')  # written before the erase
    _prepare_erasure_op(db, [("NODE_UUID", erased)])
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE failure_events SET error = 'new text' WHERE episode_uuid = ?", (erased,))
    assert _failure_rows(db, erased)[0][0] == "orig"  # content write ignored
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE failure_events SET error = ? WHERE episode_uuid = ?", (ERASED_MARKER, erased)
        )
        conn.execute("UPDATE failure_events SET details_json = NULL WHERE episode_uuid = ?", (erased,))
    assert _failure_rows(db, erased) == [(ERASED_MARKER, None)]


def test_triggers_ignore_namespace_and_session_keys(tmp_path):
    db = _db(tmp_path)
    _prepare_erasure_op(db, [("NAMESPACE", "ns-reused")])
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO mcp_events (started_at, completed_at, duration_ms, operation, kind, "
            "success, namespace, session_id, node_uuid) "
            "VALUES ('t', 't', 1, 'op', 'k', 1, 'ns-reused', 'ns-reused', NULL)"
        )
        assert conn.execute("SELECT COUNT(*) FROM mcp_events WHERE namespace='ns-reused'").fetchone()[0] == 1


def test_no_trigger_on_journal_tables_and_every_column_is_triggered_or_excluded(tmp_path):
    db = _db(tmp_path)
    with sqlite3.connect(db) as conn:
        trig_tables = {
            r[0]
            for r in conn.execute(
                "SELECT tbl_name FROM sqlite_master WHERE type='trigger' AND name LIKE ?",
                (TRIGGER_PREFIX + "%",),
            )
        }
    assert trig_tables
    assert not trig_tables & {"graph_operations", "graph_operation_subjects"}
    plan = denylist_plan()
    for entry in CONTENT_COLUMNS:
        covered = entry.column in plan.get(entry.table, ((), ()))[1]
        excluded = (entry.table, entry.column) in DENYLIST_EXCLUDED
        assert covered != excluded, (entry.table, entry.column, covered, excluded)
    assert all(DENYLIST_EXCLUDED.values()), "every exclusion needs a reason"


def test_down_migration_drops_triggers_and_is_reversible(tmp_path):
    db = _db(tmp_path)
    erased = _id()
    _prepare_erasure_op(db, [("NODE_UUID", erased)])
    with connect_telemetry_db(db) as conn:
        dropped = drop_erasure_denylist_triggers(conn)
        conn.commit()
        assert dropped > 0
        assert not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name LIKE ?", (TRIGGER_PREFIX + "%",)
        ).fetchone()
    _failure(db, erased, "now stored")  # fence is off
    assert _failure_rows(db, erased)
    with connect_telemetry_db(db) as conn:
        ensure_erasure_denylist(conn)
        conn.commit()
    _failure(db, erased, "blocked again")
    assert len(_failure_rows(db, erased)) == 1  # the earlier row, no new one
    with connect_telemetry_db(db) as conn:
        drop_erasure_denylist_triggers(conn, drop_table=True)
        conn.commit()
        assert not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='erased_subject_denylist'"
        ).fetchone()


def test_erase_reaches_committed_and_own_journal_rows_survive_triggers(tmp_path):
    db = _db(tmp_path)
    target = _id()
    _failure(db, target, "secret")
    coord = ErasureCoordinator(
        graph_adapter=_Adapter(),
        journal=GraphOperationsJournal(db_path=db),
        subjects=ErasureSubjectStore(db_path=db),
    )
    reply = coord.erase_memory(target)
    with sqlite3.connect(db) as conn:
        state = conn.execute(
            "SELECT state FROM graph_operations WHERE op_id=?", (reply["op_id"],)
        ).fetchone()[0]
    assert state == "COMMITTED"
    # Rows are blanked in place, not deleted: no row may still hold the erased text.
    assert not [r for r in _failure_rows(db, target) if "secret" in str(r)]
    assert target in _denylist(db)


# ------------------------------------------------------------------ 3. subquery purge


def test_purge_is_a_subquery_and_handles_40k_subjects(tmp_path):
    db = _db(tmp_path)
    members = [_id() for _ in range(40_000)]
    op_id = uuid.uuid4().hex
    with connect_telemetry_db(db) as conn:
        conn.executemany(
            "INSERT INTO erasure_subjects (op_id, subject_type, subject_value, recorded_at) "
            "VALUES (?, 'NODE_UUID', ?, 't')",
            [(op_id, m) for m in members],
        )
        conn.commit()
    _failure(db, members[-1], "x" * 10)
    with connect_telemetry_db(db) as conn:
        subjects = ErasureSubjects(op_id=op_id)
        result = purge_content(conn, subjects)
        assert result.rows_affected["failure_events.error"] == 1
        assert count_residual_content(conn, subjects) == {}
        conn.commit()
    assert _failure_rows(db, members[-1]) == [(ERASED_MARKER, None)]


# ------------------------------------------------------------------ 4. FLAGGED / CONTEXT_HOLDER


def test_flagged_uuid_purges_content_columns_but_keeps_row_and_is_not_denylisted(tmp_path):
    db = _db(tmp_path)
    flagged = _id()
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO memory_revisions (recorded_at, node_uuid, field, old_value, new_value, "
            "changed_by) VALUES ('t', ?, 'summary', 'old text', 'new text', 'x')",
            (flagged,),
        )
    _failure(db, flagged, "episode-keyed error")  # episode bucket is NOT addressed by FLAGGED
    op_id = _prepare_erasure_op(db, [("FLAGGED_UUID", flagged)])
    assert flagged not in _denylist(db)
    for subjects in (ErasureSubjects(op_id=op_id), ErasureSubjects(flagged_uuids=frozenset({flagged}))):
        with connect_telemetry_db(db) as conn:
            purge_content(conn, subjects)
            conn.commit()
        with sqlite3.connect(db) as conn:
            row = conn.execute(
                "SELECT old_value, new_value FROM memory_revisions WHERE node_uuid=?", (flagged,)
            ).fetchone()
        assert row == (None, None)  # row kept, content gone
    assert _failure_rows(db, flagged) == [("episode-keyed error", None)]


def test_context_holder_purges_exactly_three_columns_and_keeps_rows(tmp_path):
    db = _db(tmp_path)
    holder = _id()
    _failure(db, holder, "prompt text", '{"detail": "kept"}')
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO llm_usage_events (call_id, recorded_at, episode_uuid, kind, status, "
            "provider_usage_json, error) VALUES ('c1', 't', ?, 'chat', 'ok', '{\"p\": 1}', 'err text')",
            (holder,),
        )
    op_id = _prepare_erasure_op(db, [("CONTEXT_HOLDER_UUID", holder)])
    assert holder not in _denylist(db)
    for subjects in (
        ErasureSubjects(op_id=op_id),
        ErasureSubjects(context_holder_uuids=frozenset({holder})),
    ):
        with connect_telemetry_db(db) as conn:
            purge_content(conn, subjects)
            conn.commit()
    assert _failure_rows(db, holder) == [(ERASED_MARKER, '{"detail": "kept"}')]
    with sqlite3.connect(db) as conn:
        usage = conn.execute(
            "SELECT provider_usage_json, error FROM llm_usage_events WHERE call_id='c1'"
        ).fetchone()
    assert usage == (None, None)


# ------------------------------------------------------------------ 5. suppression


def test_suppressed_node_uuids_consults_denylist_after_purge(tmp_path):
    db = _db(tmp_path)
    erased, live = _id(), _id()
    op_id = _prepare_erasure_op(db, [("NODE_UUID", erased)])
    store = ErasureSubjectStore(db_path=db)
    assert suppressed_node_uuids(db, [erased, live]) == {erased}
    store.mark_purged(op_id)  # purged_at set: the unpurged-row predicate no longer matches
    assert suppressed_node_uuids(db, [erased, live]) == {erased}


# ------------------------------------------------------------------ 6. compaction


def _bytes(db: Path) -> bytes:
    return b"".join(
        Path(str(db) + s).read_bytes() for s in ("", "-wal", "-shm") if Path(str(db) + s).exists()
    )


def test_compaction_removes_nonsecure_deleted_and_purged_canaries_from_all_files(tmp_path):
    db = _db(tmp_path)
    legacy = f"CANARY-LEGACY-{uuid.uuid4()}"
    purged = f"CANARY-PURGED-{uuid.uuid4()}"
    target = _id()
    _failure(db, target, purged * 5)
    # A NON-secure connection writes then deletes: the bytes stay in a free page / the WAL.
    plain = sqlite3.connect(db)
    plain.execute("PRAGMA secure_delete=OFF")
    plain.execute(
        "INSERT INTO failure_events (recorded_at, operation, episode_uuid, error) "
        "VALUES ('t', 'enrich', ?, ?)",
        (_id(), legacy * 50),
    )
    plain.commit()
    plain.execute("DELETE FROM failure_events WHERE error LIKE ?", (f"%{legacy}%",))
    plain.commit()
    plain.close()
    assert legacy.encode() in _bytes(db), "precondition: the canary must be on disk before"
    assert purged.encode() in _bytes(db)

    coord = ErasureCoordinator(
        graph_adapter=_Adapter(),
        journal=GraphOperationsJournal(db_path=db),
        subjects=ErasureSubjectStore(db_path=db),
    )
    reply = coord.erase_memory(target)
    result = run_sidecar_compaction(db_path=db)
    assert result["status"] == COMPACTED, result

    blob = _bytes(db)
    assert legacy.encode() not in blob
    assert purged.encode() not in blob
    with sqlite3.connect(db) as conn:
        stamp = conn.execute(
            "SELECT sidecar_compacted_at FROM graph_operations WHERE op_id=?", (reply["op_id"],)
        ).fetchone()[0]
    assert stamp is not None


def test_compaction_stamps_only_ops_committed_before_it_started(tmp_path):
    db = _db(tmp_path)
    journal = GraphOperationsJournal(db_path=db)
    before = journal.prepare(operation_kind="EXPLICIT_ERASURE", request_json="{}")
    after = journal.prepare(operation_kind="EXPLICIT_ERASURE", request_json="{}")
    journal.mark_committed(before)
    journal.mark_committed(after)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE graph_operations SET committed_at = '9999-01-01T00:00:00+00:00' "
                     "WHERE op_id = ?", (after,))
    assert run_sidecar_compaction(db_path=db)["status"] == COMPACTED
    with sqlite3.connect(db) as conn:
        rows = dict(conn.execute("SELECT op_id, sidecar_compacted_at FROM graph_operations"))
    assert rows[before] is not None
    assert rows[after] is None


def test_compaction_returns_retry_and_stamps_nothing_when_busy(tmp_path, monkeypatch):
    db = _db(tmp_path)
    journal = GraphOperationsJournal(db_path=db)
    op = journal.prepare(operation_kind="EXPLICIT_ERASURE", request_json="{}")
    journal.mark_committed(op)
    run_sidecar_compaction(db_path=db)  # adds the column; stamps the op
    with connect_telemetry_db(db) as conn:
        conn.execute("UPDATE graph_operations SET sidecar_compacted_at = NULL")
    monkeypatch.setattr(helpers, "_SQLITE_BUSY_TIMEOUT_S", 0.05)
    blocker = sqlite3.connect(db, isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")  # holds the write lock: VACUUM cannot run
    try:
        result = run_sidecar_compaction(db_path=db)
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
    assert result["status"] == RETRY, result
    with connect_telemetry_db(db) as conn:
        assert conn.execute(
            "SELECT sidecar_compacted_at FROM graph_operations WHERE op_id=?", (op,)
        ).fetchone()[0] is None
