"""Registry-driven executor that purges content-bearing columns in the telemetry sidecar.

Deleting a memory in the graph only removes the node; the content that addresses that
subject survives in the SQLite sidecar. This module turns the classification registry
(``erasure_inventory.CONTENT_COLUMNS``) into an executor: given a set of subjects, it
purges the content those subjects address.

The purge is *registry-driven*: every table and column touched is derived from
``CONTENT_COLUMNS``, never hard-coded here. A newly classified column is therefore
purged automatically the moment it is added to the registry, and a column reclassified
from ``UNADDRESSABLE`` to a keyed shape starts being purged on the next run -- no
executor change needed. This is the whole point of keeping the classification registry
as the single source of truth.

``UNADDRESSABLE`` columns are reported rather than silently skipped. They carry content
(a memory-text preview, a raw message, an error embedding request payload) but have no
subject key, so a subject-keyed purge structurally cannot reach them. Silently ignoring
them would let a subclass of content survive an erasure with no trace; reporting them in
``PurgeResult.skipped_unaddressable`` keeps the gap visible to the caller so it can be
audited (and, eventually, closed by a schema fix).

Semantics:

- Each content column's ``key_columns`` resolve to subject sets by column name:
  ``namespace`` -> namespaces, ``episode_uuid`` -> episode_uuids, ``session_id`` ->
  session_ids, and every other key column (``node_uuid``, ``survivor_uuid``,
  ``absorbed_uuid``, ...) -> node_uuids.
- Single-key shapes (``DIRECT_SUBJECT_UUID``, ``NAMESPACE_KEYED``,
  ``DERIVABLE_SUBJECT``) match rows where that key column IS IN its subject set.
- ``TWO_PARTY_UUID`` matches rows where ANY key column matches (OR across key columns,
  never AND): matching only one side would leave recovery material for the erased
  subject, which is load-bearing, not a detail.
- Purge NULLs the content column; it never deletes rows, because rows carry non-content
  operational columns other findings depend on (erase the content, keep the shape).
- An entry whose key columns resolve to no subjects at all is skipped instead of
  emitting a ``WHERE x IN ()``.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass

from menhir.domain.erasure import ERASED_MARKER
from menhir.infrastructure.telemetry.erasure_inventory import (
    CONTENT_COLUMNS,
    ErasureShape,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ErasureSubjects:
    """The set of subjects whose content should be purged.

    Each set is drawn from by the name of a content column's key column: a key column
    named ``namespace`` uses ``namespaces``, ``episode_uuid`` uses ``episode_uuids``,
    ``session_id`` uses ``session_ids``, and every other key column (``node_uuid``,
    ``survivor_uuid``, ``absorbed_uuid``, ...) uses ``node_uuids``.
    """

    node_uuids: frozenset[str] = frozenset()
    namespaces: frozenset[str] = frozenset()
    episode_uuids: frozenset[str] = frozenset()
    session_ids: frozenset[str] = frozenset()


@dataclass(frozen=True)
class PurgeResult:
    """Outcome of a purge.

    Attributes:
        rows_affected: Mapping of ``"table.column"`` to the number of rows whose content
            was purged (or would be purged, for a dry run).
        skipped_unaddressable: ``"table.column"`` entries no subject key could reach.
    """

    rows_affected: dict[str, int]
    skipped_unaddressable: tuple[str, ...]
    #: ``"table.column"`` entries this operation resolved NO subject for, so nothing was
    #: matched. Distinct from ``skipped_unaddressable``: those columns can never be reached by
    #: anyone, these simply were not addressed by THIS erasure. Reported because the silent
    #: version of this skip is what let episode-keyed content survive a namespace erasure --
    #: the clause built empty, the entry was passed over, and the outcome looked clean.
    skipped_no_subjects: tuple[str, ...] = ()


def _subject_set_for(key_column: str, subjects: ErasureSubjects) -> frozenset[str]:
    """Resolve a key column name to the subject set that addresses it.

    Matched by suffix as well as exact name so per-party lineage columns
    (``survivor_namespace``, ``absorbed_namespace``) resolve to namespaces rather than falling
    through to the uuid default. Getting that wrong is silent: the clause would still build and
    run, just matching namespace values against a set of uuids and finding nothing.
    """
    if key_column == "namespace" or key_column.endswith("_namespace"):
        return subjects.namespaces
    if key_column == "episode_uuid":
        return subjects.episode_uuids
    if key_column == "session_id":
        return subjects.session_ids
    return subjects.node_uuids


def _where_clause_for(
    shape: ErasureShape, key_columns: tuple[str, ...], subjects: ErasureSubjects
) -> tuple[str, list[str]] | None:
    """Build a WHERE fragment + bound params for an entry, or None if unreachable.

    Returns None when no key column resolves to any subject, so the caller skips the
    entry instead of emitting a ``WHERE x IN ()``. The fragment uses ``?`` placeholders
    for all VALUES; column names are resolved here from the registry and are never
    caller input.
    """
    resolved: list[tuple[str, frozenset[str]]] = []
    for key_column in key_columns:
        values = _subject_set_for(key_column, subjects)
        if values:
            resolved.append((key_column, values))
    if not resolved:
        return None
    if shape is ErasureShape.TWO_PARTY_UUID:
        # Match on ANY key column (OR, never AND). One-sided matching would leave
        # recovery material for the erased subject.
        clauses: list[str] = []
        params: list[str] = []
        for key_column, values in resolved:
            placeholders = ",".join("?" for _ in values)
            clauses.append(f"{key_column} IN ({placeholders})")
            params.extend(values)
        return f"({' OR '.join(clauses)})", params
    key_column, values = resolved[0]
    placeholders = ",".join("?" for _ in values)
    return f"{key_column} IN ({placeholders})", list(values)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    """Whether ``table`` exists. A registry entry for a missing table holds no content.

    Skipping is correct rather than lenient: the registry is a static declaration, so it can
    legitimately name a table a given database has not created yet.
    """
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ? LIMIT 1", (table,)
    ).fetchone()
    return row is not None


def _is_not_null(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """Whether ``column`` is declared NOT NULL, so redaction must use '' rather than NULL."""
    for row in conn.execute(f"PRAGMA table_info({table})").fetchall():
        if row[1] == column:
            return bool(row[3])
    return False


def _has_content(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """Whether the column holds any populated content right now."""
    row = conn.execute(
        f"SELECT 1 FROM {table} WHERE {column} IS NOT NULL AND {column} != '' "
        f"AND {column} != ? LIMIT 1",
        (ERASED_MARKER,),
    ).fetchone()
    return row is not None


def count_residual_content(
    conn: sqlite3.Connection, subjects: ErasureSubjects
) -> dict[str, int]:
    """Count rows still holding content for ``subjects``, per "table.column".

    This is the verification step, and it is deliberately NOT ``purge_content(dry_run=True)``.
    That counts rows the subject keys MATCH, which a purge never changes -- the key column is
    not rewritten -- so it returns the same number before and after and could never witness
    success. Residual content means the content column itself is still populated.

    Redaction lands as NULL where the column allows it and as an empty string where the column
    is NOT NULL, so both count as erased.
    """
    residual: dict[str, int] = {}
    for entry in CONTENT_COLUMNS:
        if entry.shape is ErasureShape.UNADDRESSABLE:
            continue
        if not _table_exists(conn, entry.table):
            continue
        clause = _where_clause_for(entry.shape, entry.key_columns, subjects)
        if clause is None:
            continue
        where, params = clause
        # Registry-provided identifiers; every value is bound.
        # The marker counts as erased, not as residue: a NOT NULL column holds it precisely
        # because it could not hold NULL.
        row = conn.execute(
            f"SELECT COUNT(*) FROM {entry.table} WHERE {where} "
            f"AND {entry.column} IS NOT NULL AND {entry.column} != '' "
            f"AND {entry.column} != ?",
            [*params, ERASED_MARKER],
        ).fetchone()
        count = int(row[0]) if row else 0
        if count:
            residual[f"{entry.table}.{entry.column}"] = count
    return residual


_WAIVER_TABLE = "erasure_residue_waiver"


def _integer_pk(conn: sqlite3.Connection, table: str) -> str | None:
    """Name of the table's INTEGER PRIMARY KEY, or None if it has no single integer key.

    A waiver is anchored to this column, so a table without one cannot be waived at all --
    there would be nothing to bound the waiver with, and an unbounded waiver is the thing this
    design exists to prevent.
    """
    for row in conn.execute(f"PRAGMA table_info({table})"):
        if int(row[5]) == 1 and str(row[2]).upper().startswith("INT"):
            return str(row[1])
    return None


def waived_ceilings(conn: sqlite3.Connection) -> dict[str, int]:
    """Accepted-residue ceilings per ``"table.column"``: rows at or below are not reported.

    Empty when the waiver table does not exist, which is the correct reading -- no waiver has
    ever been recorded, so nothing is excluded.
    """
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (_WAIVER_TABLE,)
    ).fetchone()
    if exists is None:
        return {}
    return {
        f"{r[0]}.{r[1]}": int(r[2])
        for r in conn.execute(
            f"SELECT table_name, column_name, max_id FROM {_WAIVER_TABLE}"
        )
    }


def record_residue_waiver(conn: sqlite3.Connection, *, note: str) -> dict[str, dict]:
    """Freeze today's unaddressable rows as accepted residue. One-time operator action.

    **Why a ceiling and not a rule.** The obvious implementation of "stop reporting the legacy
    batch" is a predicate like "ignore rows whose key is NULL". That would also swallow every
    row stranded from now on, which is precisely the CF-165 defect re-introduced with a
    config-shaped lid on it. This instead records the CURRENT maximum id per column, and only
    rows at or below it are excluded. ``mcp_events.id`` is ``INTEGER PRIMARY KEY AUTOINCREMENT``,
    so SQLite never reuses an id even after deletes: a row written after this waiver is
    guaranteed a strictly higher id and is therefore guaranteed to still be reported.

    Accepting residue does not erase it. The rows keep their content; what changes is that the
    erasure outcome stops counting a known, decided-upon backlog as an open question.
    """
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_WAIVER_TABLE} (
            table_name  TEXT NOT NULL,
            column_name TEXT NOT NULL,
            max_id      INTEGER NOT NULL,
            row_count   INTEGER NOT NULL,
            accepted_at TEXT NOT NULL,
            note        TEXT NOT NULL,
            PRIMARY KEY (table_name, column_name)
        )
        """
    )
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    # Census with any existing waivers ignored, so re-running widens the ceiling to cover
    # anything that has stranded since -- an explicit operator act, never an automatic one.
    outstanding = count_unaddressable_content(conn, apply_waivers=False)
    recorded: dict[str, dict] = {}
    for key, count in outstanding.items():
        table, column = key.split(".", 1)
        pk = _integer_pk(conn, table)
        if pk is None:
            # Cannot bound it, so refuse rather than waive the whole column forever.
            logger.warning(
                "erasure residue waiver: %s has no integer primary key; leaving it reported",
                key,
            )
            continue
        row = conn.execute(f"SELECT MAX({pk}) FROM {table}").fetchone()
        max_id = int(row[0]) if row and row[0] is not None else 0
        conn.execute(
            f"INSERT INTO {_WAIVER_TABLE} "
            "(table_name, column_name, max_id, row_count, accepted_at, note) "
            "VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(table_name, column_name) DO UPDATE SET "
            "max_id=excluded.max_id, row_count=excluded.row_count, "
            "accepted_at=excluded.accepted_at, note=excluded.note",
            (table, column, max_id, count, now, note),
        )
        recorded[key] = {"max_id": max_id, "row_count": count, "accepted_at": now}
    return recorded


def count_unaddressable_content(
    conn: sqlite3.Connection, *, apply_waivers: bool = True
) -> dict[str, int]:
    """Count content-bearing rows NO subject key can ever reach, per ``"table.column"``.

    Required because ``PurgeResult.skipped_unaddressable`` does not answer this question. It
    reports entries whose declared *shape* is ``UNADDRESSABLE``, and today **no entry in the
    registry carries that shape** -- so it is always empty, and the promise in this module's
    docstring that unreachable content is "reported rather than silently skipped" currently
    reports nothing at all.

    The content that is actually stranded sits in *keyed* columns whose key is NULL: rows
    written before the lineage migration that added the key column (see the CF-165 Phase C
    notes on ``mcp_events``). Such a row matches no ``key IN (...)`` predicate, so it is
    invisible three times over -- ``purge_content`` never rewrites it, ``count_residual_content``
    never counts it, and the skipped list never names it -- and an erasure can therefore report
    success over content it did not touch.

    Counting rather than shape-listing also keeps the signal usable: a column with no subject
    key that happens to hold no content strands nothing, and must not make every erasure look
    incomplete forever.

    Takes no subject set. This is a property of the corpus, not of one erasure, so it needs no
    transactional consistency with a purge and must not run inside one -- the purge never
    rewrites a key column, so this count cannot change across it.
    """
    out: dict[str, int] = {}
    waivers = waived_ceilings(conn) if apply_waivers else {}
    for entry in CONTENT_COLUMNS:
        if not _table_exists(conn, entry.table):
            continue
        # Registry-provided identifiers; the one VALUE is bound.
        populated = (
            f"{entry.column} IS NOT NULL AND {entry.column} != '' AND {entry.column} != ?"
        )
        ceiling = waivers.get(f"{entry.table}.{entry.column}")
        if ceiling is not None:
            pk = _integer_pk(conn, entry.table)
            if pk is not None:
                # Bounded exclusion: only the rows that existed when the waiver was recorded.
                # Anything written since has a higher id and is still counted.
                populated = f"{populated} AND {pk} > {int(ceiling)}"
        if entry.shape is ErasureShape.UNADDRESSABLE:
            where = populated
        else:
            # Stranded only when EVERY key column is empty. One populated key still lets some
            # subject set address the row, so it is reachable in principle and not counted here.
            key_empty = " AND ".join(
                f"({col} IS NULL OR {col} = '')" for col in entry.key_columns
            )
            if not key_empty:
                continue
            where = f"{populated} AND {key_empty}"
        row = conn.execute(
            f"SELECT COUNT(*) FROM {entry.table} WHERE {where}", [ERASED_MARKER]
        ).fetchone()
        count = int(row[0]) if row else 0
        if count:
            out[f"{entry.table}.{entry.column}"] = count
    return out


def purge_content(
    conn: sqlite3.Connection,
    subjects: ErasureSubjects,
    *,
    dry_run: bool = False,
) -> PurgeResult:
    """Purge the content addressed by ``subjects`` across the sidecar registry.

    For every ``CONTENT_COLUMNS`` entry, NULLs the content column on rows reachable by
    the entry's key columns. ``UNADDRESSABLE`` entries are reported in
    ``skipped_unaddressable`` and never touched. Never commits; the caller owns the
    transaction (this is designed to run inside a saga).

    When ``dry_run`` is True, computes the same per-column row counts via
    ``SELECT COUNT(*)`` and performs no writes.

    Table and column names come from ``CONTENT_COLUMNS`` (never caller input), so they
    are interpolated directly into the SQL; all VALUES are bound via placeholders.
    """
    rows_affected: dict[str, int] = {}
    skipped: list[str] = []
    no_subjects: list[str] = []
    for entry in CONTENT_COLUMNS:
        key = f"{entry.table}.{entry.column}"
        if entry.shape is ErasureShape.UNADDRESSABLE:
            skipped.append(key)
            continue
        if not _table_exists(conn, entry.table):
            continue
        clause = _where_clause_for(entry.shape, entry.key_columns, subjects)
        if clause is None:
            # Only worth reporting when the column actually holds something.
            if _has_content(conn, entry.table, entry.column):
                no_subjects.append(key)
            continue
        where, params = clause
        # Table and column names are registry-provided (never caller input), so they are
        # interpolated directly; every VALUE is bound with a placeholder.
        if dry_run:
            row = conn.execute(
                f"SELECT COUNT(*) FROM {entry.table} WHERE {where}", params
            ).fetchone()
            rows_affected[key] = int(row[0])
        else:
            # A content column declared NOT NULL (merge_audit.snapshot_json) cannot be set
            # to NULL, so it gets the erasure marker: valid JSON carrying no content, which a
            # consumer can recognise. An empty string was the first approach and it made an
            # erased record indistinguishable from a corrupt one -- the parser failed either
            # way. Deleting the row is not an option either; the operational columns are
            # depended on elsewhere.
            blank = (
                ERASED_MARKER if _is_not_null(conn, entry.table, entry.column) else None
            )
            cursor = conn.execute(
                f"UPDATE {entry.table} SET {entry.column} = ? WHERE {where}",
                [blank, *params],
            )
            rows_affected[key] = cursor.rowcount
    return PurgeResult(
        rows_affected=rows_affected,
        skipped_unaddressable=tuple(skipped),
        skipped_no_subjects=tuple(no_subjects),
    )
