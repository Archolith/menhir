"""Additive schema migrations for the SQLite telemetry sidecar.

``CREATE TABLE IF NOT EXISTS`` does nothing to a table that already exists, so a sidecar
created before a column was introduced keeps its old column set forever. Columns added
after a table shipped therefore need an explicit ``PRAGMA table_info`` / ``ALTER TABLE``
pass, following the idiom in ``infrastructure/graph_operations.py``.

These live outside ``store.py`` because that module is budgeted as a thin owner of
connection + schema only (enforced by ``tests/test_large_module_boundaries.py``), and
migrations accumulate over time.

Every function here must be idempotent: ``_ensure_ready()`` runs on first use in every
process, so a migration re-runs constantly and must be a no-op once applied.
"""

from __future__ import annotations

import sqlite3


def ensure_lineage_columns(conn: sqlite3.Connection) -> None:
    """Ensure current sidecar content has a sound erasure key where one is derivable (CF-165).

    ``recall_receipts.reason`` is scrubbed instead of assigned fake ownership: a usefulness
    receipt can cover a global/workspace read, so there is no sound session->namespace mapping.

    Recall Lab is different. Its historical ``namespace=NULL`` means exactly "unscoped/default
    recall"; that maps to Menhir's reserved ``default`` namespace. Backfill is therefore provable,
    and a trigger keeps future direct-store callers from recreating NULL-keyed raw query/results.
    """
    additions: dict[str, tuple[str, ...]] = {
        "merge_audit": ("survivor_namespace", "absorbed_namespace"),
        "mcp_events": ("namespace", "node_uuid"),
        "extraction_lab_runs": ("namespace",),
    }
    for table, columns in additions.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if not existing:
            continue
        for column in columns:
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")

    recall_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(recall_receipts)").fetchall()
    }
    if "reason" in recall_columns:
        conn.execute("UPDATE recall_receipts SET reason = NULL WHERE reason IS NOT NULL")

    recall_lab_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(recall_lab_runs)").fetchall()
    }
    if "namespace" in recall_lab_columns:
        # In RecallLabRequest, None is the normal unscoped/default graph read -- unlike
        # Extraction Lab's synthetic fixtures, this ownership is deterministic.
        conn.execute(
            "UPDATE recall_lab_runs SET namespace = 'default' "
            "WHERE namespace IS NULL OR namespace = ''"
        )
        conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS trg_recall_lab_namespace_lineage
            AFTER INSERT ON recall_lab_runs
            WHEN NEW.namespace IS NULL OR NEW.namespace = ''
            BEGIN
                UPDATE recall_lab_runs SET namespace = 'default' WHERE id = NEW.id;
            END
            """
        )


__all__ = ["ensure_lineage_columns"]
