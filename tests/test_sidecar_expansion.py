"""Tests for M6 Phase 5 sidecar expansion — lifecycle_actions and memory_revisions tables."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from menhir.mcp.telemetry import McpTelemetryStore


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> McpTelemetryStore:
    """Return a fresh McpTelemetryStore pointed at a tmp_path SQLite file."""
    return McpTelemetryStore(db_path=tmp_path / "telemetry.db")


def _raw_rows(db_path: Path, table: str) -> list[sqlite3.Row]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(f"SELECT * FROM {table} ORDER BY id ASC").fetchall()


def _table_exists(db_path: Path, table: str) -> bool:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        return row[0] > 0


# ---------------------------------------------------------------------------
# 1. record_lifecycle_action inserts a row with all correct fields
# ---------------------------------------------------------------------------


def test_record_lifecycle_action_inserts_correct_fields(store: McpTelemetryStore) -> None:
    store.record_lifecycle_action(
        action="compress",
        node_uuid="node-1",
        session_id="sess-1",
        trigger="scheduler",
        before_freshness="active",
        after_freshness="compressed",
        llm_used=True,
        duration_ms=150,
        notes="scheduled compression",
    )

    rows = _raw_rows(store.db_path, "lifecycle_actions")
    assert len(rows) == 1
    row = rows[0]
    assert row["action"] == "compress"
    assert row["node_uuid"] == "node-1"
    assert row["session_id"] == "sess-1"
    assert row["trigger"] == "scheduler"
    assert row["before_freshness"] == "active"
    assert row["after_freshness"] == "compressed"
    assert row["llm_used"] == 1
    assert row["duration_ms"] == 150
    assert row["notes"] == "scheduled compression"
    assert row["recorded_at"] is not None


@pytest.mark.parametrize(
    ("action", "trigger"),
    [
        ("demote", "consolidation_demote"),  # LifecycleService consolidation demote
        ("delete", "demote_ttl_expiry"),  # LifecycleService TTL-expiry delete
    ],
)
def test_record_lifecycle_action_accepts_demote_delete(
    store: McpTelemetryStore, action: str, trigger: str
) -> None:
    """F5 demote-with-TTL action/trigger pairs must persist (regression: they were
    rejected by the validator whitelist and silently dropped)."""
    store.record_lifecycle_action(
        action=action,
        node_uuid="node-f5",
        session_id="sess-f5",
        trigger=trigger,
    )

    rows = _raw_rows(store.db_path, "lifecycle_actions")
    assert len(rows) == 1
    assert rows[0]["action"] == action
    assert rows[0]["trigger"] == trigger
    assert rows[0]["node_uuid"] == "node-f5"


# ---------------------------------------------------------------------------
# 2. record_memory_revision inserts a row with all correct fields
# ---------------------------------------------------------------------------


def test_record_memory_revision_inserts_correct_fields(store: McpTelemetryStore) -> None:
    store.record_memory_revision(
        node_uuid="node-2",
        field="content",
        old_value="old text",
        new_value="new text",
        changed_by="ingest",
        episode_uuid="ep-1",
    )

    rows = _raw_rows(store.db_path, "memory_revisions")
    assert len(rows) == 1
    row = rows[0]
    assert row["node_uuid"] == "node-2"
    assert row["field"] == "content"
    assert row["old_value"] == "old text"
    assert row["new_value"] == "new text"
    assert row["changed_by"] == "ingest"
    assert row["episode_uuid"] == "ep-1"
    assert row["recorded_at"] is not None


# ---------------------------------------------------------------------------
# 3. Append-safe — repeated calls produce distinct rows, no constraint errors
# ---------------------------------------------------------------------------


def test_lifecycle_action_append_safe(store: McpTelemetryStore) -> None:
    for i in range(3):
        store.record_lifecycle_action(
            action="compress",
            node_uuid=f"node-{i}",
            trigger="scheduler",
        )

    rows = _raw_rows(store.db_path, "lifecycle_actions")
    assert len(rows) == 3
    ids = [row["id"] for row in rows]
    assert len(set(ids)) == 3, "Each row should have a distinct auto-increment id"


def test_memory_revision_append_safe(store: McpTelemetryStore) -> None:
    for i in range(3):
        store.record_memory_revision(
            node_uuid="node-1",
            field="content",
            old_value=f"v{i}",
            new_value=f"v{i + 1}",
            changed_by="ingest",
        )

    rows = _raw_rows(store.db_path, "memory_revisions")
    assert len(rows) == 3
    ids = [row["id"] for row in rows]
    assert len(set(ids)) == 3


def test_duplicate_lifecycle_action_not_suppressed(store: McpTelemetryStore) -> None:
    """Same parameters twice should still produce two rows (no dedup logic)."""
    for _ in range(2):
        store.record_lifecycle_action(
            action="gone",
            node_uuid="node-dup",
            trigger="manual",
        )

    rows = _raw_rows(store.db_path, "lifecycle_actions")
    assert len(rows) == 2


# ---------------------------------------------------------------------------
# 4. _ensure_ready() creates the tables on a fresh DB
# ---------------------------------------------------------------------------


def test_ensure_ready_creates_tables(tmp_path: Path) -> None:
    db_path = tmp_path / "fresh.db"
    assert not db_path.exists()

    store = McpTelemetryStore(db_path=db_path)
    store._ensure_ready()

    assert db_path.exists()
    assert _table_exists(db_path, "lifecycle_actions")
    assert _table_exists(db_path, "memory_revisions")
    # The other core tables should also exist
    assert _table_exists(db_path, "mcp_events")
    assert _table_exists(db_path, "failure_events")
    assert _table_exists(db_path, "lifecycle_events")


# ---------------------------------------------------------------------------
# 5. _ensure_ready() is idempotent
# ---------------------------------------------------------------------------


def test_ensure_ready_idempotent(store: McpTelemetryStore) -> None:
    store._ensure_ready()
    # Insert a row so we can verify it survives a second _ensure_ready call
    store.record_lifecycle_action(
        action="rehydrate",
        node_uuid="node-surv",
        trigger="manual",
    )

    # Reset the initialized flag and call again
    store._initialized = False
    store._ensure_ready()

    rows = _raw_rows(store.db_path, "lifecycle_actions")
    assert len(rows) == 1
    assert rows[0]["node_uuid"] == "node-surv"


# ---------------------------------------------------------------------------
# 6. Invalid action/trigger/field/changed_by raises ValueError
# ---------------------------------------------------------------------------


def test_invalid_action_raises(store: McpTelemetryStore) -> None:
    with pytest.raises(ValueError, match="Invalid lifecycle action"):
        store.record_lifecycle_action(
            action="invalid_action",
            node_uuid="node-1",
            trigger="scheduler",
        )


def test_invalid_trigger_raises(store: McpTelemetryStore) -> None:
    with pytest.raises(ValueError, match="Invalid trigger"):
        store.record_lifecycle_action(
            action="compress",
            node_uuid="node-1",
            trigger="invalid_trigger",
        )


def test_invalid_field_raises(store: McpTelemetryStore) -> None:
    with pytest.raises(ValueError, match="Invalid revision field"):
        store.record_memory_revision(
            node_uuid="node-1",
            field="invalid_field",
            old_value="a",
            new_value="b",
            changed_by="ingest",
        )


def test_invalid_changed_by_raises(store: McpTelemetryStore) -> None:
    with pytest.raises(ValueError, match="Invalid changed_by"):
        store.record_memory_revision(
            node_uuid="node-1",
            field="content",
            old_value="a",
            new_value="b",
            changed_by="invalid_changer",
        )


def test_all_valid_actions_accepted(store: McpTelemetryStore) -> None:
    for action in ("compress", "rehydrate", "gone"):
        store.record_lifecycle_action(
            action=action,
            node_uuid=f"node-{action}",
            trigger="manual",
        )
    rows = _raw_rows(store.db_path, "lifecycle_actions")
    assert len(rows) == 3


def test_all_valid_triggers_accepted(store: McpTelemetryStore) -> None:
    for trigger in ("scheduler", "manual", "decay_sweep"):
        store.record_lifecycle_action(
            action="compress",
            node_uuid=f"node-{trigger}",
            trigger=trigger,
        )
    rows = _raw_rows(store.db_path, "lifecycle_actions")
    assert len(rows) == 3


def test_all_valid_fields_accepted(store: McpTelemetryStore) -> None:
    for field_name in ("content", "summary", "scope", "freshness"):
        store.record_memory_revision(
            node_uuid="node-1",
            field=field_name,
            old_value="old",
            new_value="new",
            changed_by="ingest",
        )
    rows = _raw_rows(store.db_path, "memory_revisions")
    assert len(rows) == 4


def test_all_valid_changed_by_accepted(store: McpTelemetryStore) -> None:
    for changer in ("ingest", "consolidation", "conflict_resolution", "decay"):
        store.record_memory_revision(
            node_uuid="node-1",
            field="content",
            old_value="old",
            new_value="new",
            changed_by=changer,
        )
    rows = _raw_rows(store.db_path, "memory_revisions")
    assert len(rows) == 4


# ---------------------------------------------------------------------------
# 7. prune_old_revisions removes rows older than retention_days
# ---------------------------------------------------------------------------


def test_prune_old_revisions_removes_old_rows(store: McpTelemetryStore) -> None:
    store._ensure_ready()

    # Insert one recent row via the normal API
    store.record_memory_revision(
        node_uuid="node-recent",
        field="content",
        old_value="a",
        new_value="b",
        changed_by="ingest",
    )

    # Manually insert an old row (30 days ago)
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            """
            INSERT INTO memory_revisions (recorded_at, node_uuid, field, old_value, new_value, changed_by)
            VALUES (datetime('now', '-30 days'), 'node-old', 'summary', 'x', 'y', 'decay')
            """,
        )
        conn.commit()

    rows_before = _raw_rows(store.db_path, "memory_revisions")
    assert len(rows_before) == 2

    deleted = store.prune_old_revisions(retention_days=14)
    assert deleted == 1

    rows_after = _raw_rows(store.db_path, "memory_revisions")
    assert len(rows_after) == 1
    assert rows_after[0]["node_uuid"] == "node-recent"


def test_prune_old_revisions_keeps_recent_rows(store: McpTelemetryStore) -> None:
    store.record_memory_revision(
        node_uuid="node-fresh",
        field="content",
        old_value="a",
        new_value="b",
        changed_by="ingest",
    )

    deleted = store.prune_old_revisions(retention_days=1)
    assert deleted == 0

    rows = _raw_rows(store.db_path, "memory_revisions")
    assert len(rows) == 1


def test_prune_old_revisions_empty_table(store: McpTelemetryStore) -> None:
    store._ensure_ready()
    deleted = store.prune_old_revisions(retention_days=1)
    assert deleted == 0


# ---------------------------------------------------------------------------
# 8. fetch_lifecycle_actions_summary returns correct action counts
# ---------------------------------------------------------------------------


def test_fetch_lifecycle_actions_summary_counts(store: McpTelemetryStore) -> None:
    # Record several lifecycle actions
    store.record_lifecycle_action(
        action="compress",
        node_uuid="node-1",
        trigger="scheduler",
        llm_used=True,
        duration_ms=100,
    )
    store.record_lifecycle_action(
        action="compress",
        node_uuid="node-2",
        trigger="scheduler",
        llm_used=False,
        duration_ms=200,
    )
    store.record_lifecycle_action(
        action="rehydrate",
        node_uuid="node-3",
        trigger="manual",
        llm_used=True,
        duration_ms=300,
    )

    # Record a memory revision too (for the revisions count)
    store.record_memory_revision(
        node_uuid="node-1",
        field="content",
        old_value="a",
        new_value="b",
        changed_by="ingest",
    )

    summary = store.fetch_lifecycle_actions_summary(since_hours=24)

    assert "compress" in summary
    assert summary["compress"]["count"] == 2
    assert summary["compress"]["avg_ms"] == 150  # (100 + 200) / 2
    assert summary["compress"]["llm_count"] == 1

    assert "rehydrate" in summary
    assert summary["rehydrate"]["count"] == 1
    assert summary["rehydrate"]["avg_ms"] == 300
    assert summary["rehydrate"]["llm_count"] == 1

    assert summary["revisions"] == 1


def test_fetch_lifecycle_actions_summary_empty(store: McpTelemetryStore) -> None:
    summary = store.fetch_lifecycle_actions_summary(since_hours=24)
    assert summary == {"revisions": 0}


def test_fetch_lifecycle_actions_summary_respects_time_window(store: McpTelemetryStore) -> None:
    store._ensure_ready()

    # Insert an action with a timestamp far in the past
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            """
            INSERT INTO lifecycle_actions
                (recorded_at, action, node_uuid, trigger, llm_used)
            VALUES (datetime('now', '-48 hours'), 'gone', 'node-old', 'decay_sweep', 0)
            """,
        )
        conn.commit()

    # Insert a recent action via the API
    store.record_lifecycle_action(
        action="compress",
        node_uuid="node-recent",
        trigger="scheduler",
        duration_ms=50,
    )

    summary = store.fetch_lifecycle_actions_summary(since_hours=24)

    # The old "gone" action should be outside the 24h window
    assert "gone" not in summary
    assert "compress" in summary
    assert summary["compress"]["count"] == 1
