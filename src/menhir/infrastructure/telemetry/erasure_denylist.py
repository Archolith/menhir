"""Erased-subject denylist and the generated content-table triggers (erasure rebuild, P3).

``erased_subject_denylist`` holds the uuids of closure subjects of an EXPLICIT_ERASURE op. A
BEFORE INSERT trigger per content-bearing table, and a BEFORE UPDATE OF <column> trigger per
content column, make a late writer (an in-flight worker, a retry) a silent no-op for an erased
subject: ``RAISE(IGNORE)`` in the same SQLite write transaction.

Design rules (plan 2.2 G-sidecar, 3.5):

* Generated from ``CONTENT_COLUMNS``; never hand-listed. A column is either triggered or named
  in ``DENYLIST_EXCLUDED`` with a reason.
* The trigger matches the row's OWN uuid subject column (``node_uuid``, ``episode_uuid``, ...).
  Namespace and session keys are never matched.
* NEVER on ``graph_operations`` or ``graph_operation_subjects``: the saga's own journal row
  indexes its target as a subject.
* The UPDATE trigger lets a *blanking* write through (NULL, '' or the erasure marker), so the
  erase's own purge is not swallowed by the denylist the same erase populated.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict

from menhir.domain.erasure import ERASED_MARKER
from menhir.infrastructure.telemetry.erasure_inventory import (
    CONTENT_COLUMNS,
    DENYLIST_EXCLUDED,
    ErasureShape,
)

DENYLIST_TABLE = "erased_subject_denylist"
TRIGGER_PREFIX = "trg_erased_deny_"
JOURNAL_TABLES = frozenset({"graph_operations", "graph_operation_subjects"})

_DENYLIST_DDL = (
    f"CREATE TABLE IF NOT EXISTS {DENYLIST_TABLE} ("
    "subject_value TEXT PRIMARY KEY, op_id TEXT)"
)


def _is_uuid_key(column: str) -> bool:
    return not (column == "namespace" or column.endswith("_namespace") or column == "session_id")


def denylist_plan() -> dict[str, tuple[tuple[str, ...], tuple[str, ...]]]:
    """``table -> (subject columns, content columns)`` for every triggered table."""
    subjects: dict[str, list[str]] = defaultdict(list)
    contents: dict[str, list[str]] = defaultdict(list)
    for entry in CONTENT_COLUMNS:
        if (entry.table, entry.column) in DENYLIST_EXCLUDED:
            continue
        if entry.table in JOURNAL_TABLES or entry.shape is ErasureShape.INDEXED_SUBJECT:
            continue
        keys = [k for k in entry.key_columns if _is_uuid_key(k)]
        if not keys:
            continue
        for key in keys:
            if key not in subjects[entry.table]:
                subjects[entry.table].append(key)
        if entry.column not in contents[entry.table]:
            contents[entry.table].append(entry.column)
    return {t: (tuple(subjects[t]), tuple(contents[t])) for t in contents}


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _denied(subject_columns: tuple[str, ...]) -> str:
    return "(" + " OR ".join(
        f"NEW.{c} IN (SELECT subject_value FROM {DENYLIST_TABLE})" for c in subject_columns
    ) + ")"


def trigger_statements(
    table: str, subject_columns: tuple[str, ...], content_columns: tuple[str, ...]
) -> list[tuple[str, str]]:
    """``(trigger name, CREATE TRIGGER sql)`` pairs for one content table. Pure: no connection."""
    marker = ERASED_MARKER.replace("'", "''")
    out = [
        (
            f"{TRIGGER_PREFIX}ins_{table}",
            f"CREATE TRIGGER IF NOT EXISTS {TRIGGER_PREFIX}ins_{table} BEFORE INSERT ON {table} "
            f"WHEN {_denied(subject_columns)} BEGIN SELECT RAISE(IGNORE); END",
        )
    ]
    for column in content_columns:
        name = f"{TRIGGER_PREFIX}upd_{table}_{column}"
        out.append(
            (
                name,
                f"CREATE TRIGGER IF NOT EXISTS {name} BEFORE UPDATE OF {column} ON {table} "
                f"WHEN {_denied(subject_columns)} AND NEW.{column} IS NOT NULL "
                f"AND NEW.{column} != '' AND NEW.{column} != '{marker}' "
                "BEGIN SELECT RAISE(IGNORE); END",
            )
        )
    return out


def ensure_erasure_denylist(conn: sqlite3.Connection) -> None:
    """Create the denylist table, then the triggers for every content table that exists.

    Idempotent. The table is created first and in the same step as the triggers, so a process
    that never readies the subject store cannot hit a trigger over a missing table. A table or
    column absent from this database is skipped (the registry may name one not yet created).
    """
    conn.execute(_DENYLIST_DDL)
    for table, (subject_cols, content_cols) in denylist_plan().items():
        existing = _table_columns(conn, table)
        if not existing:
            continue
        subjects = tuple(c for c in subject_cols if c in existing)
        contents = tuple(c for c in content_cols if c in existing)
        if not subjects or not contents:
            continue
        for _name, sql in trigger_statements(table, subjects, contents):
            conn.execute(sql)


def drop_erasure_denylist_triggers(conn: sqlite3.Connection, *, drop_table: bool = False) -> int:
    """Down-migration: drop every generated denylist trigger; optionally the denylist table.

    Returns the number of triggers dropped. The denylist rows are kept unless ``drop_table``,
    so re-running ``ensure_erasure_denylist`` restores the fence without losing it.
    """
    names = [
        str(r[0])
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE ?",
            (TRIGGER_PREFIX + "%",),
        ).fetchall()
    ]
    for name in names:
        conn.execute(f'DROP TRIGGER IF EXISTS "{name}"')
    if drop_table:
        conn.execute(f"DROP TABLE IF EXISTS {DENYLIST_TABLE}")
    return len(names)


__all__ = [
    "DENYLIST_TABLE",
    "TRIGGER_PREFIX",
    "denylist_plan",
    "drop_erasure_denylist_triggers",
    "ensure_erasure_denylist",
    "trigger_statements",
]
