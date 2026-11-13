"""Erasure must reach graph_operations snapshots (indexed through graph_operation_subjects).

Fake graph adapter, real SQLite under ``tmp_path``.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from menhir.infrastructure.erasure_subjects import ErasureSubjectStore
from menhir.infrastructure.graph_operations import GraphOperationsJournal
from menhir.infrastructure.graph_operations_prepare import _snapshot_subject_uuids
from menhir.infrastructure.telemetry.store import McpTelemetryStore
from menhir.services.erasure_coordinator import (
    ERASED,
    NOTHING_TO_ERASE,
    RESIDUAL_CONTENT,
    ErasureCoordinator,
)

pytestmark = [pytest.mark.unit]

OLD = "2000-01-01T00:00:00+00:00"


class FakeAdapter:
    def __init__(self, *, present: bool = True, fail_once: str | None = None) -> None:
        self.present = present
        self.fail_once = fail_once
        self.deleted_nodes: list[str] = []

    def delete_memory(self, node_uuid: str) -> bool:
        if self.fail_once is not None:
            message, self.fail_once = self.fail_once, None
            raise RuntimeError(message)
        self.deleted_nodes.append(node_uuid)
        return self.present


def _coordinator(tmp_path, adapter=None) -> ErasureCoordinator:
    db = tmp_path / "t.db"
    McpTelemetryStore(db_path=db)._ensure_ready()
    return ErasureCoordinator(
        graph_adapter=adapter or FakeAdapter(),
        journal=GraphOperationsJournal(db_path=db),
        subjects=ErasureSubjectStore(db_path=db),
    )


def _delete_snapshot(node: str, peer: str) -> str:
    return json.dumps(
        {"nodes": {node: {"uuid": node, "relationships": [{"peer_uuid": peer}]}}}
    )


def _seed_op(
    coord, *, kind, request, snapshot, state="COMMITTED", updated_at=None, target_uuid=None
) -> str:
    """Journal a row through the real prepare(), then settle it to ``state``.

    Participant locks are dropped for terminal states so they do not fence the erasure.
    """
    op_id = coord.journal.prepare(
        operation_kind=kind,
        request_json=json.dumps(request),
        before_snapshot_json=snapshot,
        target_uuid=target_uuid,
    )
    with sqlite3.connect(coord.journal.db_path) as conn:
        conn.execute("UPDATE graph_operations SET state = ? WHERE op_id = ?", (state, op_id))
        if updated_at:
            conn.execute(
                "UPDATE graph_operations SET updated_at = ? WHERE op_id = ?", (updated_at, op_id)
            )
        if state != "PREPARED":
            conn.execute("DELETE FROM graph_operation_locks WHERE op_id = ?", (op_id,))
        conn.commit()
    return op_id


def _snapshot(db, op_id):
    with sqlite3.connect(db) as conn:
        return conn.execute(
            "SELECT before_snapshot_json FROM graph_operations WHERE op_id = ?", (op_id,)
        ).fetchone()[0]


def _state(db, op_id):
    with sqlite3.connect(db) as conn:
        return conn.execute(
            "SELECT state FROM graph_operations WHERE op_id = ?", (op_id,)
        ).fetchone()[0]


# ------------------------------------------------------------------ 1-3 subject index


def _row_token(db, op_id):
    with sqlite3.connect(db) as conn:
        return conn.execute(
            "SELECT owner_token FROM graph_operations WHERE op_id = ?", (op_id,)
        ).fetchone()[0]


def test_snapshot_subject_uuids_delete_shape_includes_peers():
    out = _snapshot_subject_uuids({"targets": ["u1"]}, _delete_snapshot("u1", "p1"))
    assert out == {"u1", "p1"}


def test_snapshot_subject_uuids_merge_shape():
    snap = json.dumps(
        {
            "body": {
                "survivor": {"uuid": "s", "relationships": [{"peer_uuid": "sp"}]},
                "absorbed": {"uuid": "a"},
            }
        }
    )
    assert _snapshot_subject_uuids({}, snap) == {"s", "sp", "a"}


def test_snapshot_subject_uuids_malformed_returns_request_uuids():
    request = {"survivor_uuid": "s", "absorbed_uuid": "a", "targets": ["t"]}
    assert _snapshot_subject_uuids(request, "{not json") == {"s", "a", "t"}
    assert _snapshot_subject_uuids(request, "[1, 2]") == {"s", "a", "t"}
    assert _snapshot_subject_uuids(request, None) == {"s", "a", "t"}


def test_prepare_indexes_target_request_and_snapshot_uuids(tmp_path):
    coord = _coordinator(tmp_path)
    op_id = _seed_op(
        coord,
        kind="ENTITY_DELETE",
        request={"targets": ["r1"]},
        snapshot=_delete_snapshot("u1", "p1"),
        target_uuid="t1",
    )
    with sqlite3.connect(tmp_path / "t.db") as conn:
        rows = {
            r[0]
            for r in conn.execute(
                "SELECT subject_uuid FROM graph_operation_subjects WHERE op_id = ?", (op_id,)
            )
        }
    assert rows == {"t1", "r1", "u1", "p1"}


def test_backfill_indexes_preexisting_rows_once(tmp_path):
    db = tmp_path / "t.db"
    GraphOperationsJournal(db_path=db)._ensure_ready()
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO graph_operations (op_id, operation_kind, target_uuid, request_json, "
            "before_snapshot_json, state, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                "legacy-op", "ENTITY_DELETE", "t1", json.dumps({"targets": ["r1"]}),
                _delete_snapshot("u1", "p1"), "COMMITTED", OLD, OLD,
            ),
        )
        # Simulate a database written before the index existed.
        conn.execute("DROP TABLE graph_operation_subjects")
        conn.execute("DROP TABLE graph_operation_subjects_backfill")
        conn.commit()

    GraphOperationsJournal(db_path=db)._ensure_ready()

    with sqlite3.connect(db) as conn:
        subjects = {
            r[0]
            for r in conn.execute(
                "SELECT subject_uuid FROM graph_operation_subjects WHERE op_id = 'legacy-op'"
            )
        }
        assert subjects == {"t1", "r1", "u1", "p1"}
        assert conn.execute(
            "SELECT COUNT(*) FROM graph_operation_subjects_backfill"
        ).fetchone()[0] == 1
        # Prove the marker stops a rerun: wipe the index, re-init, it must stay empty.
        conn.execute("DELETE FROM graph_operation_subjects")
        conn.commit()

    GraphOperationsJournal(db_path=db)._ensure_ready()

    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM graph_operation_subjects").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM graph_operation_subjects_backfill"
        ).fetchone()[0] == 1


# ------------------------------------------------------------------ 4-6 purge behaviour


def test_erasure_purges_committed_delete_snapshot_only_for_the_subject(tmp_path):
    coord = _coordinator(tmp_path)
    db = tmp_path / "t.db"
    hit = _seed_op(
        coord, kind="ENTITY_DELETE", request={"targets": ["u1"]},
        snapshot=_delete_snapshot("u1", "p1"),
    )
    other = _seed_op(
        coord, kind="ENTITY_DELETE", request={"targets": ["x9"]},
        snapshot=_delete_snapshot("x9", "y9"),
    )

    out = coord.erase_memory("u1")

    assert out["reason"] == ERASED
    assert out["purged"]["graph_operations.before_snapshot_json"] == 1
    assert _snapshot(db, hit) is None
    assert _snapshot(db, other) is not None


def test_prepared_merge_snapshot_is_not_purged_and_erasure_quarantines(tmp_path):
    """An unresolved merge needs its snapshot to replay, so it cannot be purged.

    The erasure must then refuse to claim the content is gone.
    """
    coord = _coordinator(tmp_path)
    db = tmp_path / "t.db"
    snap = json.dumps(
        {"body": {"survivor": {"uuid": "s1", "relationships": [{"peer_uuid": "erase-me"}]},
                  "absorbed": {"uuid": "a1"}}}
    )
    merge = _seed_op(
        coord, kind="ENTITY_MERGE",
        request={"survivor_uuid": "s1", "absorbed_uuid": "a1"},
        snapshot=snap, state="PREPARED",
    )

    out = coord.erase_memory("erase-me")

    assert out["reason"] == RESIDUAL_CONTENT
    assert "graph_operations.before_snapshot_json" in out["residual"]
    assert _snapshot(db, merge) == snap
    assert _state(db, out["op_id"]) == "NEEDS_REVIEW"


def test_already_null_snapshot_is_not_reported_as_purged(tmp_path):
    coord = _coordinator(tmp_path, FakeAdapter(present=False))
    _seed_op(
        coord, kind="ENTITY_DELETE", request={"targets": ["u1"]}, snapshot=None,
    )

    out = coord.erase_memory("u1")

    assert out["reason"] == NOTHING_TO_ERASE
    assert out["purged"].get("graph_operations.before_snapshot_json", 0) == 0


# ------------------------------------------------------------------ 7 own-row last_error


def test_own_row_last_error_is_cleared_after_replay(tmp_path):
    secret = "SECRET-CONTENT-IN-EXCEPTION"
    adapter = FakeAdapter(fail_once=f"boom {secret}")
    coord = _coordinator(tmp_path, adapter)
    db = tmp_path / "t.db"

    with pytest.raises(RuntimeError):
        coord.erase_memory("n-1")

    with sqlite3.connect(db) as conn:
        op_id, last_error = conn.execute(
            "SELECT op_id, last_error FROM graph_operations "
            "WHERE operation_kind = 'EXPLICIT_ERASURE'"
        ).fetchone()
    assert secret in last_error  # precondition: the attempt really stored it

    outcome, _ = coord.replay_prepared_row(
        {"op_id": op_id, "operation_kind": "EXPLICIT_ERASURE"},
        owner_token=_row_token(db, op_id),
    )

    assert outcome == "REPLAYED"
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT last_error FROM graph_operations WHERE op_id = ?", (op_id,)
        ).fetchone()[0] is None


# ------------------------------------------------------------------ 8 merge irreversibility


class _NoNeo4j:
    def execute(self, *_a, **_k):
        return []


class _NoLegacyAudit:
    def fetch_merge_audit(self, **_k):
        return []


def _merge_snapshot(survivor: str, absorbed: str) -> str:
    return json.dumps({"body": {"survivor": {"uuid": survivor}, "absorbed": {"uuid": absorbed}}})


def test_erased_merge_snapshot_blocks_unmerge_and_is_inventoried(tmp_path):
    from menhir.services.merge_recoverability import ERASED_IRREVERSIBLE, EXACT, inventory
    from menhir.services.unmerge_coordinator import (
        NO_SNAPSHOT,
        SNAPSHOT_ERASED,
        UnmergeCoordinator,
    )

    coord = _coordinator(tmp_path)
    erased_merge = _seed_op(
        coord, kind="ENTITY_MERGE",
        request={"survivor_uuid": "s1", "absorbed_uuid": "a1"},
        snapshot=_merge_snapshot("s1", "a1"),
    )
    intact_merge = _seed_op(
        coord, kind="ENTITY_MERGE",
        request={"survivor_uuid": "s2", "absorbed_uuid": "a2"},
        snapshot=_merge_snapshot("s2", "a2"),
    )
    # Never erased, snapshot simply missing: must stay distinguishable from erasure.
    missing_merge = _seed_op(
        coord, kind="ENTITY_MERGE",
        request={"survivor_uuid": "s3", "absorbed_uuid": "a3"}, snapshot=None,
    )

    coord.erase_memory("a1")
    assert _snapshot(tmp_path / "t.db", erased_merge) is None

    unmerge = UnmergeCoordinator(graph_adapter=object(), journal=coord.journal)
    assert unmerge.unmerge(erased_merge)["reason"] == SNAPSHOT_ERASED
    assert unmerge.unmerge(missing_merge)["reason"] == NO_SNAPSHOT

    report = inventory(neo4j=_NoNeo4j(), journal=coord.journal, telemetry_store=_NoLegacyAudit())
    assert report.counts[ERASED_IRREVERSIBLE] == 1
    assert report.counts[EXACT] == 1
    erased_rows = [r for r in report.rows if r["tier"] == ERASED_IRREVERSIBLE]
    assert [r["merge_op_id"] for r in erased_rows] == [erased_merge]
    exact_rows = [r for r in report.rows if r["tier"] == EXACT]
    assert [r["merge_op_id"] for r in exact_rows] == [intact_merge]


# ------------------------------------------------------------------ 9 retention


def test_prune_nulls_only_old_terminal_delete_snapshots(tmp_path):
    coord = _coordinator(tmp_path)
    db = tmp_path / "t.db"
    old_delete = _seed_op(
        coord, kind="ENTITY_DELETE", request={"targets": ["d1"]},
        snapshot=_delete_snapshot("d1", "p"), updated_at=OLD,
    )
    recent_delete = _seed_op(
        coord, kind="ENTITY_DELETE", request={"targets": ["d2"]},
        snapshot=_delete_snapshot("d2", "p"),
    )
    old_merge = _seed_op(
        coord, kind="ENTITY_MERGE", request={"survivor_uuid": "s", "absorbed_uuid": "a"},
        snapshot=_merge_snapshot("s", "a"), updated_at=OLD,
    )
    old_prepared_delete = _seed_op(
        coord, kind="ENTITY_DELETE", request={"targets": ["d3"]},
        snapshot=_delete_snapshot("d3", "p"), state="PREPARED", updated_at=OLD,
    )

    deleted = McpTelemetryStore(db_path=db).prune_telemetry_tables(
        observability_days=1, diagnostic_days=1
    )

    assert deleted["graph_operations.before_snapshot_json"] == 1
    assert _snapshot(db, old_delete) is None
    assert _snapshot(db, recent_delete) is not None
    assert _snapshot(db, old_merge) is not None
    assert _snapshot(db, old_prepared_delete) is not None


# ------------------------------------------------------------------ 11 replay after a failed drain


class _MergeAuditAdapter:
    """Graph side of an erasure whose target is cited in one entity's merge_audit.

    ``scrub_merge_audit_for_episodes`` behaves like the real one: the first call tombstones the
    entry and reports the absorbed uuid; a second call finds nothing, because the tombstone no
    longer contains the erased uuid (memory_queries.scrub_merge_audit_for_episodes)."""

    def __init__(self, absorbed: str) -> None:
        self.absorbed = absorbed
        self.scrubs = 0

    def delete_memory_detailed(self, node_uuid, *, erased_uuids=None, deletable_entity_uuids=None):
        return {"touched": True}

    def scrub_merge_audit_for_episodes(self, erased_uuids, deleted_entity_uuids=None):
        self.scrubs += 1
        if self.scrubs > 1:
            return {"merge_audits_tombstoned": 0, "merge_absorbed_uuids": []}
        return {"merge_audits_tombstoned": 1, "merge_absorbed_uuids": [self.absorbed]}


def test_absorbed_entity_snapshot_is_purged_when_first_erase_stays_prepared(
    tmp_path, monkeypatch
):
    """The absorbed entity's merge snapshot holds text; its uuid is only discovered by the graph
    delete's merge_audit scrub. If the telemetry drain then fails, the erasure stays PREPARED and
    the replay's scrub finds nothing (already tombstoned). Fails if the discovered uuid is only
    recorded after the drain, so the replay purges nothing for it and the snapshot row, with its
    phrase, survives a committed erasure."""
    from menhir.infrastructure.telemetry.async_writer import TELEMETRY_WRITER

    phrase = "absorbed-entity-phrase-7731"
    adapter = _MergeAuditAdapter(absorbed="abs-1")
    coord = _coordinator(tmp_path, adapter)
    db = tmp_path / "t.db"
    merge = _seed_op(
        coord,
        kind="ENTITY_MERGE",
        request={"survivor_uuid": "surv-1", "absorbed_uuid": "abs-1"},
        snapshot=json.dumps(
            {"body": {"survivor": {"uuid": "surv-1"},
                      "absorbed": {"uuid": "abs-1", "summary": phrase}}}
        ),
    )
    assert phrase in _snapshot(db, merge)  # precondition: the row holds the phrase

    calls = {"n": 0}

    def drain_fails_once(*_a, **_k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("telemetry drain timed out")

    monkeypatch.setattr(TELEMETRY_WRITER, "drain_strict", drain_fails_once)

    with pytest.raises(RuntimeError):
        coord.erase_memory("erased-ep-1")
    with sqlite3.connect(db) as conn:
        op_id, state = conn.execute(
            "SELECT op_id, state FROM graph_operations WHERE operation_kind = 'EXPLICIT_ERASURE'"
        ).fetchone()
    assert state == "PREPARED"  # precondition: the first erase did not finish

    outcome, _ = coord.replay_prepared_row(
        {"op_id": op_id, "operation_kind": "EXPLICIT_ERASURE"},
        owner_token=_row_token(db, op_id),
    )

    assert outcome == "REPLAYED"
    assert _state(db, op_id) == "COMMITTED"
    snap = _snapshot(db, merge)
    assert snap is None or phrase not in snap


# ------------------------------------------------------------------ 10 secure_delete


def test_erasure_connection_enables_secure_delete(tmp_path):
    coord = _coordinator(tmp_path)
    conn = coord._connect()
    try:
        assert conn.execute("PRAGMA secure_delete").fetchone()[0] == 1
    finally:
        conn.close()
