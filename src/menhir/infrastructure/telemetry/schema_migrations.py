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


def ensure_merge_audit_namespace_columns(conn: sqlite3.Connection) -> None:
    """Add ``survivor_namespace`` / ``absorbed_namespace`` to ``merge_audit`` (CF-165).

    An explicit namespace erasure must be able to find the merge recovery rows belonging
    to that namespace. Without durable lineage the namespace could only be recovered by
    parsing ``snapshot_json`` or by assuming the survivor's graph node still exists --
    neither survives the deletion this exists to support.

    Nullable with no default: rows written before this migration genuinely have no
    recorded namespace, and NULL is what the read and write paths expect for them.
    Backfill is deliberately not attempted -- it is only sound where derivation is
    provable, which is a separate decision.
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(merge_audit)").fetchall()}
    for column in ("survivor_namespace", "absorbed_namespace"):
        if column not in existing:
            # Literal names from the tuple above; never caller input.
            conn.execute(f"ALTER TABLE merge_audit ADD COLUMN {column} TEXT")


__all__ = ["ensure_merge_audit_namespace_columns"]
