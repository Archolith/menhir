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
    """Add subject-lineage columns to every telemetry table that needs them (CF-165).

    Content-bearing rows are only erasable when a subject key reaches them. Tables
    carrying memory or user text therefore need durable lineage columns:
    ``merge_audit`` gets ``survivor_namespace`` / ``absorbed_namespace`` so a namespace
    erasure can find its merge recovery rows (the namespace could otherwise only be
    recovered by parsing ``snapshot_json`` or assuming the survivor's graph node still
    exists -- neither survives the deletion this exists to support), ``mcp_events`` gets
    ``namespace`` / ``node_uuid``, and ``extraction_lab_runs`` gets ``namespace``.

    Nullable with no default: rows written before this migration genuinely have no
    recorded lineage, and NULL is what the read and write paths expect for them.
    Backfill is deliberately not attempted -- it is only sound where derivation is
    provable, which is a separate decision.

    ``recall_receipts.reason`` is intentionally scrubbed here. A usefulness receipt is
    session-wide and can describe a global/workspace recall, so inventing namespace ownership
    would be unsound. The structured score remains; the optional prose is not needed for the
    metric and was the only content-bearing field that made namespace erasure depend on a
    session->namespace mapping that does not exist.
    """
    additions: dict[str, tuple[str, ...]] = {
        "merge_audit": ("survivor_namespace", "absorbed_namespace"),
        "mcp_events": ("namespace", "node_uuid"),
        "extraction_lab_runs": ("namespace",),
    }
    for table, columns in additions.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if not existing:
            # Table not created yet. Callers run this after the CREATE TABLE block, but
            # skipping keeps a reordering from turning into an ALTER on a missing table.
            continue
        for column in columns:
            if column not in existing:
                # Literal names from the mapping above; never caller input.
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")

    recall_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(recall_receipts)").fetchall()
    }
    if "reason" in recall_columns:
        # Idempotent privacy migration: old free-text rating notes have no sound namespace
        # lineage. Keep the structured score/label and remove only the optional prose.
        conn.execute("UPDATE recall_receipts SET reason = NULL WHERE reason IS NOT NULL")


__all__ = ["ensure_lineage_columns"]
