"""Unit tests for the registry-driven telemetry sidecar content purge.

Builds a real sidecar under ``tmp_path`` via ``McpTelemetryStore`` and exercises
``purge_content`` against fixture rows inserted with direct SQL. Never touches the real
telemetry database. The expected ``UNADDRESSABLE`` set is derived from
``CONTENT_COLUMNS`` rather than hard-coded, because another phase is changing which
entries are unaddressable.
"""

from __future__ import annotations

import sqlite3

import pytest

from menhir.infrastructure.telemetry.erasure_inventory import (
    ContentColumn,
    CONTENT_COLUMNS,
    ErasureShape,
)
from menhir.infrastructure.telemetry.erasure_purge import (
    ErasureSubjects,
    purge_content,
)
from menhir.infrastructure.telemetry.store import McpTelemetryStore

pytestmark = [pytest.mark.unit]


_LEGACY_SEED_GUARDS = (
    "trg_cf165_mcp_missing_lineage",
    "trg_cf165_merge_infer_lineage",
    "trg_cf165_merge_drop_unowned",
)


def _sidecar(tmp_path) -> tuple[McpTelemetryStore, sqlite3.Connection]:
    """Create a real sidecar under tmp_path and return (store, connection).

    This module intentionally inserts historical/pre-lineage rows with raw SQL. Disable the
    current forward guards in this isolated fixture so those rows remain representable; the
    persistence-boundary tests separately prove new application writes cannot recreate them.
    """
    store = McpTelemetryStore(db_path=tmp_path / "t.db")
    store._ensure_ready()
    conn = sqlite3.connect(store.db_path)
    for trigger in _LEGACY_SEED_GUARDS:
        conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    conn.commit()
    return store, conn


def _insert_memory_revision(conn, recorded_at, node_uuid, old_value, new_value):
    conn.execute(
        "INSERT INTO memory_revisions "
        "(recorded_at, node_uuid, field, old_value, new_value, changed_by, episode_uuid) "
        "VALUES (?,?,?,?,?,?,?)",
        (recorded_at, node_uuid, "field", old_value, new_value, "sys", None),
    )


def _insert_merge_audit(conn, survivor_uuid, absorbed_uuid, snapshot_json):
    conn.execute(
        "INSERT INTO merge_audit "
        "(recorded_at, survivor_uuid, absorbed_uuid, snapshot_json) "
        "VALUES (?,?,?,?)",
        ("t", survivor_uuid, absorbed_uuid, snapshot_json),
    )


def _insert_recall_lab_run(conn, namespace, query):
    conn.execute(
        "INSERT INTO recall_lab_runs "
        "(recorded_at, query, preset, namespace, judge_enabled, tied_ids_json, "
        "arms_json, request_json, result_json) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        ("t", query, "preset", namespace, 1, "[]", "arms", "request", "result"),
    )


def _erased(value) -> bool:
    """Content is gone. NULL where the column allows it, the erasure marker where it does not.

    merge_audit.snapshot_json is declared NOT NULL, so it cannot be set to NULL. It gets the
    marker instead -- valid JSON carrying no content -- so a consumer can tell an erasure from
    a corrupt row, which an empty string could not.
    """
    from menhir.domain.erasure import is_erased_marker

    return value is None or is_erased_marker(value)


class TestErasurePurge:
    def test_direct_subject_nulled_and_untargeted_untouched(self, tmp_path):
        _store, conn = _sidecar(tmp_path)
        with conn:
            _insert_memory_revision(conn, "t", "target-uuid", "old", "new")
            _insert_memory_revision(conn, "t", "untargeted-uuid", "keep-old", "keep-new")

        result = purge_content(
            conn, ErasureSubjects(node_uuids=frozenset({"target-uuid"}))
        )

        assert result.rows_affected["memory_revisions.old_value"] == 1
        assert result.rows_affected["memory_revisions.new_value"] == 1

        row = conn.execute(
            "SELECT old_value, new_value FROM memory_revisions WHERE node_uuid=?",
            ("target-uuid",),
        ).fetchone()
        assert row == (None, None)

        row = conn.execute(
            "SELECT old_value, new_value FROM memory_revisions WHERE node_uuid=?",
            ("untargeted-uuid",),
        ).fetchone()
        assert row == ("keep-old", "keep-new")

    def test_two_party_one_sided_matching(self, tmp_path):
        _store, conn = _sidecar(tmp_path)
        with conn:
            _insert_merge_audit(conn, "surv-target", "unrelated-absorbed", '{"surv":1}')
            _insert_merge_audit(conn, "unrelated-survivor", "abs-target", '{"abs":1}')
            _insert_merge_audit(
                conn, "unrelated-survivor", "unrelated-absorbed", '{"none":1}'
            )

        result = purge_content(
            conn,
            ErasureSubjects(node_uuids=frozenset({"surv-target", "abs-target"})),
        )

        # Both one-sided rows are nulled; only the row with neither side matches survives.
        assert result.rows_affected["merge_audit.snapshot_json"] == 2

        val = conn.execute(
            "SELECT snapshot_json FROM merge_audit "
            "WHERE survivor_uuid=? AND absorbed_uuid=?",
            ("surv-target", "unrelated-absorbed"),
        ).fetchone()[0]
        assert _erased(val)

        val = conn.execute(
            "SELECT snapshot_json FROM merge_audit "
            "WHERE survivor_uuid=? AND absorbed_uuid=?",
            ("unrelated-survivor", "abs-target"),
        ).fetchone()[0]
        assert _erased(val)

        val = conn.execute(
            "SELECT snapshot_json FROM merge_audit "
            "WHERE survivor_uuid=? AND absorbed_uuid=?",
            ("unrelated-survivor", "unrelated-absorbed"),
        ).fetchone()[0]
        assert val == '{"none":1}'

    def test_namespace_keyed(self, tmp_path):
        _store, conn = _sidecar(tmp_path)
        with conn:
            _insert_recall_lab_run(conn, "ns-target", "q-target")
            _insert_recall_lab_run(conn, "ns-other", "q-other")

        result = purge_content(
            conn, ErasureSubjects(namespaces=frozenset({"ns-target"}))
        )

        for col in ("query", "arms_json", "request_json", "result_json"):
            assert result.rows_affected[f"recall_lab_runs.{col}"] == 1

        row = conn.execute(
            "SELECT query, arms_json, request_json, result_json "
            "FROM recall_lab_runs WHERE namespace=?",
            ("ns-target",),
        ).fetchone()
        assert all(_erased(v) for v in row)

        row = conn.execute(
            "SELECT query, arms_json, request_json, result_json "
            "FROM recall_lab_runs WHERE namespace=?",
            ("ns-other",),
        ).fetchone()
        assert row == ("q-other", "arms", "request", "result")

    def test_unaddressable_is_reported_and_never_guessed_at(self, tmp_path, monkeypatch):
        """The UNADDRESSABLE path must stay exercised even when the registry has none.

        Phase C gave the last two unaddressable tables durable lineage, so deriving the
        expectation from the live CONTENT_COLUMNS now yields an empty set and asserts
        nothing. A synthetic entry keeps the branch honest: content with no reachable
        key must be REPORTED, never touched, and never given an invented key.
        """
        _store, conn = _sidecar(tmp_path)
        with conn:
            conn.execute(
                "INSERT INTO mcp_events "
                "(started_at, completed_at, duration_ms, operation, kind, success, "
                "payload_preview, error) VALUES (?,?,?,?,?,?,?,?)",
                ("s", "e", 1, "op", "k", 1, "preview", "err"),
            )

        synthetic = ContentColumn(
            table="mcp_events",
            column="payload_preview",
            shape=ErasureShape.UNADDRESSABLE,
            key_columns=(),
            note="synthetic fixture",
        )
        monkeypatch.setattr(
            "menhir.infrastructure.telemetry.erasure_purge.CONTENT_COLUMNS", (synthetic,)
        )

        result = purge_content(
            conn, ErasureSubjects(node_uuids=frozenset({"any-uuid"}))
        )

        assert result.skipped_unaddressable == ("mcp_events.payload_preview",)
        assert "mcp_events.payload_preview" not in result.rows_affected
        assert conn.execute("SELECT payload_preview FROM mcp_events").fetchone()[0] == "preview"

    def test_dry_run_matches_real_counts_and_writes_nothing(self, tmp_path):
        _store, conn = _sidecar(tmp_path)
        with conn:
            _insert_memory_revision(conn, "t", "target", "old", "new")
            _insert_memory_revision(conn, "t", "untargeted", "keep", "keep")
            _insert_merge_audit(conn, "surv-target", "unrelated", '{"s":1}')

        subjects = ErasureSubjects(node_uuids=frozenset({"target", "surv-target"}))
        dry = purge_content(conn, subjects, dry_run=True)

        # Content intact after a dry run.
        row = conn.execute(
            "SELECT old_value FROM memory_revisions WHERE node_uuid=?", ("target",)
        ).fetchone()[0]
        assert row == "old"

        real = purge_content(conn, subjects, dry_run=False)
        assert dry.rows_affected == real.rows_affected
        assert dry.skipped_unaddressable == real.skipped_unaddressable
        assert real.rows_affected["memory_revisions.old_value"] == 1

    def test_empty_subjects_is_noop(self, tmp_path):
        _store, conn = _sidecar(tmp_path)
        with conn:
            _insert_memory_revision(conn, "t", "target", "old", "new")

        result = purge_content(conn, ErasureSubjects())

        assert result.rows_affected == {}
        row = conn.execute(
            "SELECT old_value FROM memory_revisions WHERE node_uuid=?", ("target",)
        ).fetchone()[0]
        assert row == "old"

    def test_rows_preserved_not_deleted(self, tmp_path):
        _store, conn = _sidecar(tmp_path)
        with conn:
            _insert_memory_revision(conn, "t", "target", "old", "new")
            _insert_memory_revision(conn, "t", "untargeted", "keep", "keep")

        before = conn.execute("SELECT COUNT(*) FROM memory_revisions").fetchone()[0]
        assert before == 2

        purge_content(conn, ErasureSubjects(node_uuids=frozenset({"target"})))

        after = conn.execute("SELECT COUNT(*) FROM memory_revisions").fetchone()[0]
        assert after == before
