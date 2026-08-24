"""Subject-data export: produce a machine-readable copy of everything menhir holds (CF-180).

CF-180 recorded that no export path exists. What it also established is that the hard part was
already built twice, for deletion: an exhaustive map of where one subject's data lives. This module
is the READ counterpart of that map, not a new subsystem.

  * sidecar (SQLite): `erasure_inventory.CONTENT_COLUMNS` -- 21 content columns across 11 tables,
    each declaring the subject keys that address it.
  * graph (Neo4j): every label, exported whole.

Three properties this export must have, in descending order of how badly getting them wrong would
hurt:

1. **It must not serve content an erasure is withholding.** Between a committed erasure intent and
   a finished purge the rows still hold their text, and the `erasure_subjects` table is the
   read-suppression record for exactly that window. The full suppressed set is read up front --
   NOT "which of these candidates is suppressed" -- because a candidate-scoped check can only
   suppress things it was asked about, and under a namespace filter the far endpoint of an edge is
   never among the candidates. If the suppression state cannot be read, this **refuses to export at
   all**: the one failure mode worse than no export is an export of content someone asked to have
   erased.
2. **It must state its own coverage.** 34% of sidecar content rows carry NULL in their only key
   column and are unreachable by any subject set. `count_unaddressable_content` already measures
   that, so the manifest reports it rather than the export quietly inheriting it. Owner ruling
   2026-08-23: ship with a stated gap rather than closing it first.
3. **It must never write.** Enforced rather than asserted: the sidecar is opened through a
   `mode=ro` URI, so a write is refused by SQLite itself. This matters more than it looks --
   `ErasureSubjectStore.has_live_erasure` calls `_ensure_ready`, which issues `CREATE TABLE` and
   `CREATE INDEX` and `mkdir`s the parent directory. Using the obvious helper would have made an
   "export" mutate the operator's telemetry database on a path that does not exist yet.

Scope, per owner ruling: the whole store by default, with an OPTIONAL namespace filter. The filter
is a convenience for extracting one project, NOT a subject boundary -- there is one human subject
here, and namespaces are project scopes (`archolith`, `yawn`, `menhir`). A filtered bundle is
narrowed on BOTH sides: graph edges are kept only when both endpoints are in scope, so the bundle
has no dangling references, and a sidecar table that cannot be narrowed by any key this filter
knows is OMITTED rather than exported whole. Over-exporting another project's content into a
scoped extract is the direction that leaks, so omission is the safe side of that trade and the
manifest names every table it dropped.

**Known limit, stated because the code does not enforce it:** `Neo4jRepository.execute` returns
materialized rows, so the graph halves are read fully into memory before being written. At the
current corpus (52k nodes, 148k relationships) that is fine; this is not a design that scales to an
arbitrarily large graph, and making it stream needs a repository that yields.
"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Properties excluded from every exported row: derived vectors, not content. A 1536-float array per
#: node would dominate the bundle while carrying nothing a reader could use -- they are recomputable
#: from the text that IS exported. Named here rather than filtered ad hoc so the manifest can state
#: exactly what was dropped.
EXCLUDED_PROPERTIES: frozenset[str] = frozenset(
    {"name_embedding", "content_embedding", "fact_embedding"}
)

#: Namespace expression, identical to the one `merge_eligibility` and the tenancy predicates use.
_NAMESPACE_EXPR = "coalesce({var}.namespace, {var}.group_id, 'default')"


class ExportRefused(Exception):
    """Raised when the export cannot prove it is safe to read. Never a partial bundle."""


@dataclass
class SuppressionState:
    """Subjects covered by a committed but unpurged erasure. Read whole, not per-candidate."""

    node_uuids: frozenset[str] = frozenset()
    namespaces: frozenset[str] = frozenset()
    episode_uuids: frozenset[str] = frozenset()
    session_ids: frozenset[str] = frozenset()

    @property
    def any_live(self) -> bool:
        return bool(
            self.node_uuids or self.namespaces or self.episode_uuids or self.session_ids
        )


@dataclass
class ExportCoverage:
    """What the bundle contains, and -- more importantly -- what it does not."""

    unaddressable_rows: dict[str, int] = field(default_factory=dict)
    unaddressable_total: int = 0
    suppressed_namespaces: list[str] = field(default_factory=list)
    omitted_sidecar_tables: dict[str, str] = field(default_factory=dict)
    excluded_properties: list[str] = field(default_factory=list)
    namespace_filter: str | None = None
    notes: list[str] = field(default_factory=list)


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean(properties: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in properties.items() if k not in EXCLUDED_PROPERTIES}


def _write_jsonl(path: Path, rows) -> int:
    """Write rows to JSONL one at a time. Returns the count written.

    The WRITE is incremental; the READ that feeds it is not (see the module docstring).
    """
    written = 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, default=str, ensure_ascii=False) + "\n")
            written += 1
    return written


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    """Open the sidecar in a mode where a write is impossible, not merely unintended.

    A read-only URI is what makes the module's "never writes" claim provable. The alternative --
    using ErasureSubjectStore -- issues DDL through `_ensure_ready` and would have an export
    creating tables and directories in the operator's telemetry store.
    """
    if not db_path.exists():
        raise ExportRefused(f"sidecar not found at {db_path}")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _read_suppression_state(db_path: Path) -> SuppressionState:
    """Read every live erasure subject. Fails CLOSED by refusing, never by returning empty.

    An empty return and an unreadable store are indistinguishable to the caller, and one of them
    means "export everything" -- so the unreadable case must raise instead.
    """
    try:
        conn = _connect_readonly(db_path)
    except ExportRefused:
        raise
    except Exception as exc:  # noqa: BLE001 - refusing is the point; the cause is reported
        raise ExportRefused(f"cannot open the sidecar to read suppression state: {exc}") from exc

    try:
        with conn:
            present = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='erasure_subjects' LIMIT 1"
            ).fetchone()
            if present is None:
                # A valid sidecar that has never recorded an erasure. Distinguishable from an
                # unreadable store precisely because the query above SUCCEEDED.
                return SuppressionState()
            rows = conn.execute(
                "SELECT subject_type, subject_value FROM erasure_subjects WHERE purged_at IS NULL"
            ).fetchall()
    except ExportRefused:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ExportRefused(
            "cannot read erasure suppression state, so this export cannot prove it would not "
            f"include content a pending erasure is withholding: {exc}"
        ) from exc
    finally:
        conn.close()

    by_type: dict[str, set[str]] = {}
    for row in rows:
        by_type.setdefault(str(row[0]), set()).add(str(row[1]))
    return SuppressionState(
        node_uuids=frozenset(by_type.get("NODE_UUID", set())),
        namespaces=frozenset(by_type.get("NAMESPACE", set())),
        episode_uuids=frozenset(by_type.get("EPISODE_UUID", set())),
        session_ids=frozenset(by_type.get("SESSION_ID", set())),
    )


def export_subject_data(
    neo4j,
    *,
    output_dir: Path | str,
    telemetry_db_path: Path | str | None = None,
    namespace: str | None = None,
) -> dict[str, Any]:
    """Write a bundle of everything menhir holds. Read-only; returns the manifest.

    Args:
        neo4j: a `Neo4jRepository`-shaped object exposing `execute(query, params)`.
        output_dir: directory the bundle is written to. Created if absent.
        telemetry_db_path: sidecar path. Defaults to the configured telemetry DB.
        namespace: optional project-scope filter. NOT a subject boundary; see the module docstring.
    """
    from menhir.infrastructure.telemetry import default_telemetry_db_path

    out = Path(output_dir)
    db_path = Path(telemetry_db_path) if telemetry_db_path else Path(default_telemetry_db_path())

    coverage = ExportCoverage(
        excluded_properties=sorted(EXCLUDED_PROPERTIES), namespace_filter=namespace
    )

    # (1) Suppression first, read WHOLE. Nothing is read from the graph until this holds.
    suppression = _read_suppression_state(db_path)
    coverage.suppressed_namespaces = sorted(suppression.namespaces)
    if namespace is not None and namespace in suppression.namespaces:
        raise ExportRefused(
            f"namespace {namespace!r} is under a live, unpurged erasure; exporting it would serve "
            "content that erasure is withholding"
        )

    wrote_anything = False
    try:
        manifest = _export(neo4j, out, db_path, namespace, suppression, coverage)
        wrote_anything = True
        return manifest
    except Exception:
        # A bundle without a manifest still LOOKS like a record. If any stage failed, leave nothing
        # behind rather than a directory a reader could mistake for a complete export.
        if not wrote_anything and out.exists():
            shutil.rmtree(out, ignore_errors=True)
        raise


def _export(neo4j, out: Path, db_path: Path, namespace, suppression, coverage) -> dict[str, Any]:
    ns_node = _NAMESPACE_EXPR.format(var="n")

    # (2) Graph nodes. Every label, whole. Structural rows are counted separately rather than
    #     dropped -- a silent omission is the failure this export exists to avoid.
    node_rows = neo4j.execute(
        f"""
        MATCH (n)
        {f"WHERE {ns_node} = $namespace" if namespace else ""}
        RETURN coalesce(n.uuid, '') AS uuid, labels(n) AS labels,
               {ns_node} AS namespace,
               n.structure_role IS NOT NULL AS structural,
               properties(n) AS properties
        """,
        {"namespace": namespace} if namespace else {},
    )

    def _is_suppressed(uuid: str, ns: str) -> bool:
        return uuid in suppression.node_uuids or ns in suppression.namespaces

    kept_nodes = [
        r
        for r in node_rows
        if not _is_suppressed(str(r.get("uuid") or ""), str(r.get("namespace") or ""))
    ]
    withheld = len(node_rows) - len(kept_nodes)
    node_count = _write_jsonl(
        out / "graph_nodes.jsonl",
        (
            {
                "uuid": r["uuid"],
                "labels": r["labels"],
                "namespace": r["namespace"],
                "structural": bool(r["structural"]),
                "properties": _clean(dict(r["properties"] or {})),
            }
            for r in kept_nodes
        ),
    )
    structural_count = sum(1 for r in kept_nodes if r.get("structural"))
    exported_uuids = {str(r["uuid"]) for r in kept_nodes if r.get("uuid")}
    episode_uuids = {
        str(r["uuid"]) for r in kept_nodes if "Episodic" in (r.get("labels") or [])
    }

    # (3) Relationships, filtered on BOTH endpoints. Filtering only the start node would export
    #     edges whose end_uuid names a node absent from the bundle -- dangling references in a
    #     record that is supposed to stand alone -- and, under a namespace filter, would carry the
    #     edge's `fact` text about a node outside the scope, including a suppressed one.
    ns_pair = f"{_NAMESPACE_EXPR.format(var='n')} = $namespace AND {_NAMESPACE_EXPR.format(var='m')} = $namespace"
    rel_rows = neo4j.execute(
        f"""
        MATCH (n)-[r]->(m)
        {f"WHERE {ns_pair}" if namespace else ""}
        RETURN type(r) AS type, coalesce(n.uuid, '') AS start_uuid,
               coalesce(m.uuid, '') AS end_uuid,
               {_NAMESPACE_EXPR.format(var='n')} AS start_namespace,
               {_NAMESPACE_EXPR.format(var='m')} AS end_namespace,
               properties(r) AS properties
        """,
        {"namespace": namespace} if namespace else {},
    )

    def _edge_ok(r) -> bool:
        start, end = str(r.get("start_uuid") or ""), str(r.get("end_uuid") or "")
        if _is_suppressed(start, str(r.get("start_namespace") or "")):
            return False
        if _is_suppressed(end, str(r.get("end_namespace") or "")):
            return False
        # Closure: never reference a node the bundle does not contain.
        return not (start and start not in exported_uuids) and not (
            end and end not in exported_uuids
        )

    rel_count = _write_jsonl(
        out / "graph_relationships.jsonl",
        (
            {
                "type": r["type"],
                "start_uuid": r["start_uuid"],
                "end_uuid": r["end_uuid"],
                "properties": _clean(dict(r["properties"] or {})),
            }
            for r in rel_rows
            if _edge_ok(r)
        ),
    )

    # (4) Sidecar, driven by the erasure registry so a new content column joins the export the
    #     moment it is classified.
    sidecar_counts, unaddressable, omitted = _export_sidecar(
        db_path, out, namespace, exported_uuids, episode_uuids, suppression, coverage
    )
    coverage.unaddressable_rows = unaddressable
    coverage.unaddressable_total = sum(unaddressable.values())
    coverage.omitted_sidecar_tables = omitted

    coverage.notes = [
        "Embeddings are excluded: derived vectors, recomputable from the exported text.",
        "unaddressable_rows counts content rows NO subject key can reach (CF-165); they are "
        "operational telemetry, and no unaddressable row exceeds 1,000 characters.",
        "structural_nodes are code-structure index rows from the project scanner, not authored "
        "content. They are included and counted, never silently dropped.",
        "Relationships are included only when BOTH endpoints are in the bundle, so no exported "
        "edge references a node this bundle does not contain.",
    ]
    if namespace is not None:
        coverage.notes.append(
            "NAMESPACE-FILTERED BUNDLE: a project scope, not a subject boundary. This is not a "
            "complete record of everything menhir holds about the subject."
        )
        if omitted:
            coverage.notes.append(
                "Sidecar tables that cannot be narrowed by this filter were OMITTED rather than "
                "exported whole, so the bundle does not carry other namespaces' content: "
                + ", ".join(sorted(omitted))
            )
    if coverage.suppressed_namespaces:
        coverage.notes.append(
            "Namespaces under a live unpurged erasure were excluded: "
            + ", ".join(coverage.suppressed_namespaces)
        )
    if withheld:
        coverage.notes.append(
            f"{withheld} node(s) withheld: covered by a committed but unpurged erasure."
        )

    manifest = {
        "created_at": _utc_stamp(),
        "graph_nodes": node_count,
        "graph_nodes_structural": structural_count,
        "graph_relationships": rel_count,
        "sidecar_rows": sidecar_counts,
        "sidecar_rows_total": sum(sidecar_counts.values()),
        "withheld_node_uuids": withheld,
        "coverage": asdict(coverage),
    }
    (out / "manifest.json").write_text(
        json.dumps(manifest, indent=2, default=str), encoding="utf-8"
    )
    logger.info(
        "subject export written to %s: %d nodes, %d relationships, %d sidecar rows",
        out, node_count, rel_count, manifest["sidecar_rows_total"],
    )
    return manifest


def _export_sidecar(
    db_path: Path,
    out: Path,
    namespace: str | None,
    node_uuids: set[str],
    episode_uuids: set[str],
    suppression: SuppressionState,
    coverage: ExportCoverage,
) -> tuple[dict[str, int], dict[str, int], dict[str, str]]:
    """Export every classified content column, one JSONL per table.

    Rows are emitted whole (all columns), not just the content column: a content value without the
    row that carried it is not a record of anything.

    Under a namespace filter each table is narrowed by whichever of its registry-declared key
    columns this filter can resolve. A table with no resolvable key is OMITTED and named in the
    manifest -- exporting it whole would put other projects' content in a scoped extract.
    """
    from menhir.infrastructure.telemetry.erasure_inventory import CONTENT_COLUMNS
    from menhir.infrastructure.telemetry.erasure_purge import count_unaddressable_content

    tables: dict[str, set[str]] = {}
    for entry in CONTENT_COLUMNS:
        tables.setdefault(entry.table, set()).update(entry.key_columns)

    counts: dict[str, int] = {}
    omitted: dict[str, str] = {}
    conn = _connect_readonly(db_path)
    try:
        with conn:
            unaddressable = count_unaddressable_content(conn)
            for table, key_columns in sorted(tables.items()):
                if not _table_exists(conn, table):
                    continue
                clauses, params = _subject_clauses(
                    conn, table, key_columns, namespace, node_uuids, episode_uuids
                )
                if namespace is not None and not clauses:
                    omitted[table] = (
                        "no key column this namespace filter can resolve; exporting it whole "
                        "would include other namespaces' content"
                    )
                    continue
                # Withhold rows addressed by a suppressed subject, the same way the graph half does.
                supp_clauses, supp_params = _suppression_clauses(conn, table, suppression)
                where_parts = ([" OR ".join(clauses)] if clauses else []) + supp_clauses
                where = f" WHERE {' AND '.join(f'({c})' for c in where_parts)}" if where_parts else ""
                rows = conn.execute(
                    f"SELECT * FROM {table}{where}",  # noqa: S608 - table/column names from registry
                    params + supp_params,
                )
                counts[table] = _write_jsonl(
                    out / f"sidecar_{table}.jsonl", (dict(r) for r in rows)
                )
    finally:
        conn.close()
    return counts, unaddressable, omitted


def _subject_clauses(conn, table, key_columns, namespace, node_uuids, episode_uuids):
    """Positive selection under a namespace filter. Empty when nothing can be resolved."""
    if namespace is None:
        return [], []
    clauses: list[str] = []
    params: list[Any] = []
    resolvable = {
        "namespace": [namespace],
        "node_uuid": sorted(node_uuids),
        "survivor_uuid": sorted(node_uuids),
        "absorbed_uuid": sorted(node_uuids),
        "survivor_namespace": [namespace],
        "absorbed_namespace": [namespace],
        "episode_uuid": sorted(episode_uuids),
    }
    for column in sorted(key_columns):
        values = resolvable.get(column)
        if not values or not _column_exists(conn, table, column):
            continue
        clauses.append(f"{column} IN ({','.join('?' * len(values))})")
        params.extend(values)
    return clauses, params


def _suppression_clauses(conn, table, suppression: SuppressionState):
    """Exclude rows addressed by a subject under a live erasure."""
    clauses: list[str] = []
    params: list[Any] = []
    for column, values in (
        ("node_uuid", suppression.node_uuids),
        ("survivor_uuid", suppression.node_uuids),
        ("absorbed_uuid", suppression.node_uuids),
        ("namespace", suppression.namespaces),
        ("episode_uuid", suppression.episode_uuids),
        ("session_id", suppression.session_ids),
    ):
        if not values or not _column_exists(conn, table, column):
            continue
        ordered = sorted(values)
        clauses.append(
            f"{column} IS NULL OR {column} NOT IN ({','.join('?' * len(ordered))})"
        )
        params.extend(ordered)
    return clauses, params


def _table_exists(conn, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1", (table,)
    ).fetchone()
    return row is not None


def _column_exists(conn, table: str, column: str) -> bool:
    return any(r[1] == column for r in conn.execute(f"PRAGMA table_info({table})"))
