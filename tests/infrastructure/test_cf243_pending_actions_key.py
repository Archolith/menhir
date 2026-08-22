"""CF-243: `pending_actions` was keyed on `node_uuid` alone.

`action` was a column, not part of the key, so one node could hold only ONE pending action.
Enqueuing a second kind overwrote the first -- including its `attempts`, `failed_at` and
`failure_reason`, so a row accumulating failures was reset to a clean row of a different kind --
and `complete(node_uuid)` deleted whichever had won.

Both losses were silent: the upsert reported success and `complete()` reported a delete.

Two kinds exist in production, `compress` and `rehydrate`, and a node can legitimately be pending
both -- a node compressed and later queued for rehydration is the ordinary lifecycle.
"""

from __future__ import annotations

import sqlite3

import pytest

from menhir.infrastructure.pending_actions import PendingActionStore, _migrate_pending_actions_key

pytestmark = pytest.mark.unit


def _store(tmp_path) -> PendingActionStore:
    PendingActionStore._initialized_paths.clear()
    return PendingActionStore(db_path=tmp_path / "t.db")


# ---------------------------------------------------------------------------
# the finding
# ---------------------------------------------------------------------------


def test_two_action_kinds_coexist_for_one_node(tmp_path) -> None:
    """THE FINDING. Under the old key the second upsert overwrote the first."""
    s = _store(tmp_path)
    s.upsert("node-1", "compress")
    s.upsert("node-1", "rehydrate")

    kinds = {r["action"] for r in s.fetch_pending()}
    assert kinds == {"compress", "rehydrate"}


def test_the_second_kind_does_not_reset_the_first_ones_attempts(tmp_path) -> None:
    """The quieter half of the loss: the overwrite carried the NEW kind's attempts, so a row that
    had been failing repeatedly came back looking fresh."""
    s = _store(tmp_path)
    s.upsert("node-1", "compress", failure_reason="llm_failed")
    s.upsert("node-1", "compress", failure_reason="llm_failed")
    s.upsert("node-1", "rehydrate")

    by_kind = {r["action"]: r for r in s.fetch_pending()}
    assert by_kind["compress"]["attempts"] == 2
    assert by_kind["compress"]["failure_reason"] == "llm_failed"
    assert by_kind["rehydrate"]["attempts"] == 1


def test_completing_one_kind_leaves_the_other(tmp_path) -> None:
    """`complete(node_uuid)` used to delete by node alone, discharging an action the caller had
    not finished."""
    s = _store(tmp_path)
    s.upsert("node-1", "compress")
    s.upsert("node-1", "rehydrate")

    assert s.complete("node-1", "rehydrate") is True

    remaining = [r["action"] for r in s.fetch_pending()]
    assert remaining == ["compress"]


# ---------------------------------------------------------------------------
# positive controls
# ---------------------------------------------------------------------------


def test_the_same_kind_still_upserts_rather_than_duplicating(tmp_path) -> None:
    """POSITIVE CONTROL: the composite key must not turn repeat failures into duplicate rows --
    that is what the attempts counter is for."""
    s = _store(tmp_path)
    s.upsert("node-1", "compress", failure_reason="a")
    s.upsert("node-1", "compress", failure_reason="b")

    rows = s.fetch_pending()
    assert len(rows) == 1
    assert rows[0]["attempts"] == 2


def test_complete_without_an_action_still_clears_everything(tmp_path) -> None:
    """POSITIVE CONTROL for the optional argument: a caller that genuinely means "this node is
    done with everything" keeps the pre-CF-243 behaviour."""
    s = _store(tmp_path)
    s.upsert("node-1", "compress")
    s.upsert("node-1", "rehydrate")

    assert s.complete("node-1") is True
    assert s.fetch_pending() == []


def test_completing_a_kind_that_is_not_pending_reports_false(tmp_path) -> None:
    s = _store(tmp_path)
    s.upsert("node-1", "compress")

    assert s.complete("node-1", "rehydrate") is False
    assert len(s.fetch_pending()) == 1


# ---------------------------------------------------------------------------
# the migration
# ---------------------------------------------------------------------------


def _legacy_table(path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE pending_actions (
            node_uuid       TEXT PRIMARY KEY,
            action          TEXT NOT NULL,
            context         TEXT,
            source_uuid     TEXT,
            attempts        INTEGER DEFAULT 0,
            failed_at       TEXT,
            failure_reason  TEXT,
            created_at      TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT INTO pending_actions (node_uuid, action, attempts, created_at) VALUES (?,?,?,?)",
        ("legacy-1", "compress", 3, "2026-01-01T00:00:00+00:00"),
    )
    conn.commit()
    return conn


def test_an_existing_table_is_migrated_not_left_on_the_old_key(tmp_path) -> None:
    """CREATE TABLE IF NOT EXISTS does NOTHING to a table that already exists, so shipping the new
    schema alone would leave every existing deployment on the single-column key."""
    db = tmp_path / "legacy.db"
    conn = _legacy_table(db)

    assert _migrate_pending_actions_key(conn) is True

    key = sorted((c[5], c[1]) for c in conn.execute("PRAGMA table_info(pending_actions)") if c[5])
    assert [n for _, n in key] == ["node_uuid", "action"]


def test_the_migration_preserves_existing_rows(tmp_path) -> None:
    """A migration that silently dropped the backlog would look like a clean success."""
    conn = _legacy_table(tmp_path / "legacy.db")
    _migrate_pending_actions_key(conn)

    row = conn.execute("SELECT node_uuid, action, attempts FROM pending_actions").fetchone()
    assert row == ("legacy-1", "compress", 3)


def test_the_migration_is_idempotent(tmp_path) -> None:
    """It runs on every startup, so a second pass must be a no-op rather than a rebuild."""
    conn = _legacy_table(tmp_path / "legacy.db")

    assert _migrate_pending_actions_key(conn) is True
    assert _migrate_pending_actions_key(conn) is False
