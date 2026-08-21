"""Lineage backfill for sidecar rows written before CF-165 (operator tool).

The lineage columns CF-165 added are nullable, so every row written before the migration has
NULL lineage and is unreachable by a subject-keyed purge. This closes that gap where it can be
closed **provably**, and refuses to guess where it cannot.

Two cases, and the difference between them is the whole design:

* **`merge_audit` namespaces are derivable.** Merge candidate selection requires both
  participants to share a namespace (``correlation_queries``: ``coalesce(survivor.namespace,
  survivor.group_id, 'default') = coalesce(absorbed.namespace, absorbed.group_id, 'default')``),
  so if the survivor node still exists its namespace is provably the namespace of BOTH sides.
  If the survivor is gone, nothing proves it and the row stays NULL.

* **`mcp_events` and `extraction_lab_runs` rows are NOT derivable.** They carry memory text and
  never recorded a subject, and nothing in the row identifies whose content it is. There is no
  derivation to perform, only a choice: retain content that no erasure can ever reach, or redact
  it. The CF-165 plan is explicit -- "the compliance-safe fallback is conservative
  redaction/deletion of that unaddressable content, not retention based on guesswork".

Redaction is therefore offered but never automatic, and never bundled with the derivable half.
It destroys historical telemetry, so it is a separate opt-in with a dry run in front of it.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

#: Content columns on tables that recorded no subject before CF-165. Rows with NULL lineage in
#: these tables can never be addressed, so they are the redaction candidates.
_LEGACY_UNADDRESSABLE: dict[str, tuple[str, ...]] = {
    "mcp_events": ("payload_preview", "error"),
    "extraction_lab_runs": ("current_message", "arms_json", "request_json", "result_json"),
}

#: The MCP operations whose recorded payload carries USER CONTENT rather than operational shape.
#:
#: Owner ruling 2026-08-21: *"There is very little value in preserving historical raw
#: prompt/memory snippets inside an observability sidecar."* Measured on the live sidecar, these
#: five account for 6,131 of 218,921 rows; everything else is scheduler, enrichment and identity
#: telemetry whose previews are operation shape (`{"max_age_hours": 4.0}`) and carry nothing to
#: erase.
#:
#: DELIBERATELY NOT the same selector as `_LEGACY_UNADDRESSABLE`. That one redacts rows whose
#: LINEAGE is NULL -- 214,969 rows spanning 78 operations on this deployment -- which is the
#: opposite of the ruling: it would mutate ~209k operational rows for no privacy gain while
#: leaving a content row that happens to HAVE lineage untouched. Content-bearing and
#: unaddressable are different properties and they get different functions.
CONTENT_BEARING_OPERATIONS: tuple[str, ...] = (
    "add_memory",
    "add_memory_and_track",
    "recall_memories",
    "build_context",
    "ingest_document",
)


@dataclass
class BackfillReport:
    """What the backfill found and what it would do. Rendered for an operator."""

    dry_run: bool = True
    merge_audit_null: int = 0
    merge_audit_derivable: int = 0
    merge_audit_written: int = 0
    merge_audit_underivable: int = 0
    unaddressable_rows: dict[str, int] = field(default_factory=dict)
    redacted: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "merge_audit_null": self.merge_audit_null,
            "merge_audit_derivable": self.merge_audit_derivable,
            "merge_audit_written": self.merge_audit_written,
            "merge_audit_underivable": self.merge_audit_underivable,
            "unaddressable_rows": self.unaddressable_rows,
            "redacted": self.redacted,
        }

    def render(self) -> str:
        lines = [
            f"CF-165 lineage backfill ({'DRY RUN' if self.dry_run else 'APPLIED'})",
            f"  merge_audit rows missing lineage : {self.merge_audit_null}",
            f"  derivable from a live survivor   : {self.merge_audit_derivable}",
            f"  written                          : {self.merge_audit_written}",
            f"  underivable (survivor gone)      : {self.merge_audit_underivable}",
        ]
        if self.unaddressable_rows:
            lines.append("  legacy rows no erasure can reach:")
            for key, count in sorted(self.unaddressable_rows.items()):
                lines.append(f"    {key}: {count}")
        if self.redacted:
            lines.append("  redacted:")
            for key, count in sorted(self.redacted.items()):
                lines.append(f"    {key}: {count}")
        elif self.unaddressable_rows:
            lines.append("  (pass --redact-unaddressable to erase the above; it is irreversible)")
        return "\n".join(lines)


def backfill_merge_audit_namespaces(
    conn: sqlite3.Connection, graph_adapter: Any, *, dry_run: bool = True
) -> BackfillReport:
    """Fill `merge_audit` namespace lineage where the surviving node still proves it.

    Only rows whose survivor is still present in the graph are written. A missing survivor means
    the namespace is not provable from anything that survives, and an unprovable namespace is
    worse than a NULL one: it would make a purge believe it had covered content it never saw.
    """
    report = BackfillReport(dry_run=dry_run)
    rows = conn.execute(
        "SELECT id, survivor_uuid, absorbed_uuid FROM merge_audit "
        "WHERE survivor_namespace IS NULL OR absorbed_namespace IS NULL"
    ).fetchall()
    report.merge_audit_null = len(rows)
    if not rows:
        return report

    survivors = [str(r[1]) for r in rows if r[1]]
    namespaces = graph_adapter.fetch_node_namespaces(survivors)

    updates: list[tuple[str, str, int]] = []
    for row_id, survivor_uuid, _absorbed_uuid in rows:
        namespace = namespaces.get(str(survivor_uuid))
        if not namespace:
            report.merge_audit_underivable += 1
            continue
        # Both sides get the same value: the merge could not have happened across namespaces.
        updates.append((namespace, namespace, int(row_id)))

    report.merge_audit_derivable = len(updates)
    if dry_run or not updates:
        return report

    conn.executemany(
        "UPDATE merge_audit SET survivor_namespace = ?, absorbed_namespace = ? WHERE id = ?",
        updates,
    )
    report.merge_audit_written = len(updates)
    return report


def survey_unaddressable_legacy(conn: sqlite3.Connection) -> dict[str, int]:
    """Count legacy rows carrying content that no subject key can reach."""
    counts: dict[str, int] = {}
    for table, columns in _LEGACY_UNADDRESSABLE.items():
        if not _table_exists(conn, table):
            continue
        key_columns = [c for c in ("node_uuid", "namespace") if _column_exists(conn, table, c)]
        if not key_columns:
            continue
        missing_lineage = " AND ".join(f"{c} IS NULL" for c in key_columns)
        has_content = " OR ".join(
            f"({c} IS NOT NULL AND {c} != '')"
            for c in columns
            if _column_exists(conn, table, c)
        )
        if not has_content:
            continue
        row = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {missing_lineage} AND ({has_content})"
        ).fetchone()
        count = int(row[0]) if row else 0
        if count:
            counts[table] = count
    return counts


def redact_unaddressable_legacy(
    conn: sqlite3.Connection, *, dry_run: bool = True
) -> dict[str, int]:
    """Redact content in legacy rows that no erasure could ever reach.

    Irreversible, and deliberately not part of the derivable backfill. These rows carry memory
    text with no subject, so they can never be erased on request; retaining them means an
    erasure that reports success has left content behind. The plan's stated fallback is to
    redact rather than retain on guesswork -- but that destroys historical telemetry, so the
    caller has to ask for it explicitly.

    Only rows whose lineage is NULL are touched. A row that HAS lineage is reachable by a normal
    purge and is none of this function's business.
    """
    redacted: dict[str, int] = {}
    for table, columns in _LEGACY_UNADDRESSABLE.items():
        if not _table_exists(conn, table):
            continue
        key_columns = [c for c in ("node_uuid", "namespace") if _column_exists(conn, table, c)]
        if not key_columns:
            continue
        missing_lineage = " AND ".join(f"{c} IS NULL" for c in key_columns)
        for column in columns:
            if not _column_exists(conn, table, column):
                continue
            where = f"{missing_lineage} AND {column} IS NOT NULL AND {column} != ''"
            if dry_run:
                row = conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE {where}"
                ).fetchone()
                count = int(row[0]) if row else 0
            else:
                count = conn.execute(
                    f"UPDATE {table} SET {column} = NULL WHERE {where}"
                ).rowcount
            if count:
                redacted[f"{table}.{column}"] = count
    return redacted


def redact_content_operations(
    conn: sqlite3.Connection,
    *,
    dry_run: bool = True,
    operations: tuple[str, ...] = CONTENT_BEARING_OPERATIONS,
) -> dict[str, int]:
    """Redact the stored payload of telemetry rows for content-bearing OPERATIONS.

    Irreversible. Sets `payload_preview` and `error` to NULL on `mcp_events` rows whose
    `operation` is one of *operations*, regardless of lineage.

    **Why lineage is not part of the predicate.** A content row that happens to carry a
    namespace is still a verbatim copy of a user's memory text or search query sitting in an
    observability sidecar; that it *could* be reached by a subject purge is not a reason to keep
    it. Conversely the 209k lineage-less operational rows carry no content and redacting them
    destroys telemetry for nothing. The selector follows the content, which is the thing being
    protected.

    Rows already redacted are matched too and simply write NULL over `[redacted]`, so re-running
    is idempotent rather than an error.
    """
    if not operations:
        return {}
    redacted: dict[str, int] = {}
    table = "mcp_events"
    if not _table_exists(conn, table):
        return {}
    placeholders = ",".join("?" for _ in operations)
    # TWO SELECTORS, because content reaches this table in two shapes.
    #
    # (1) `operation` is the content tool itself -- `add_memory`, `recall_memories`, ...
    # (2) the row is a GATEWAY call whose preview WRAPS one: the operation is `memory_gateway`
    #     and the payload is `{"action": "add_memory", "payload_json": "{\"text\": ...}"}`.
    #
    # Selector (1) alone misses (2) entirely, and it did: a first pass over operations left 611
    # rows holding verbatim memory text and search queries under `memory_gateway`. Matching the
    # embedded `"text":` / `"query":` field catches content by SHAPE, so a future action name --
    # the gateway calls recall `recall`, not `recall_memories` -- cannot slip past a name list.
    # Measured precise on the live sidecar: all 611 matches are `memory_gateway`, no other
    # operation false-positives.
    envelope = f'({{col}} LIKE ? OR {{col}} LIKE ?)'
    env_params = ('%\\"text\\":%', '%\\"query\\":%')
    for column in ("payload_preview", "error"):
        if not _column_exists(conn, table, column):
            continue
        where = (
            f"({column} IS NOT NULL AND {column} != '') AND ("
            f"operation IN ({placeholders}) OR {envelope.format(col=column)})"
        )
        params = tuple(operations) + env_params
        if dry_run:
            row = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {where}", params
            ).fetchone()
            count = int(row[0]) if row else 0
        else:
            count = conn.execute(
                f"UPDATE {table} SET {column} = NULL WHERE {where}", params
            ).rowcount
        if count:
            redacted[f"{table}.{column}"] = count
    return redacted


def run_backfill(
    conn: sqlite3.Connection,
    graph_adapter: Any,
    *,
    dry_run: bool = True,
    redact_unaddressable: bool = False,
) -> BackfillReport:
    """Derive what can be derived, survey what cannot, and redact only if asked."""
    report = backfill_merge_audit_namespaces(conn, graph_adapter, dry_run=dry_run)
    report.unaddressable_rows = survey_unaddressable_legacy(conn)
    if redact_unaddressable:
        report.redacted = redact_unaddressable_legacy(conn, dry_run=dry_run)
    if not dry_run:
        conn.commit()
    logger.info("CF-165 lineage backfill:\n%s", report.render())
    return report


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ? LIMIT 1", (table,)
        ).fetchone()
        is not None
    )


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(r[1] == column for r in conn.execute(f"PRAGMA table_info({table})").fetchall())


__all__ = [
    "BackfillReport",
    "backfill_merge_audit_namespaces",
    "redact_unaddressable_legacy",
    "redact_content_operations",
    "CONTENT_BEARING_OPERATIONS",
    "run_backfill",
    "survey_unaddressable_legacy",
]
