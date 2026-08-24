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
   a finished purge the rows still hold their text, and `ErasureSubjectStore.has_live_erasure` is
   the read-suppression predicate that exists for exactly that window. An export is a reader like
   any other. If the suppression state cannot be read, this **refuses to export at all** rather
   than guessing -- the one failure mode worse than no export is an export of content someone asked
   to have erased.
2. **It must state its own coverage.** 34% of sidecar content rows carry NULL in their only key
   column and are unreachable by any subject set. `count_unaddressable_content` already measures
   that, so the manifest reports it rather than the export quietly inheriting it. Owner ruling
   2026-08-23: ship with a stated gap rather than closing it first.
3. **It must never write.** Every statement here is a read.

Scope, per owner ruling: the whole store by default, with an OPTIONAL namespace filter. The filter
is a convenience for extracting one project, NOT a subject boundary -- there is one human subject
here, and namespaces are project scopes (`archolith`, `yawn`, `menhir`). The manifest says so, so a
namespace-filtered bundle is never mistaken for a complete subject record.
"""

from __future__ import annotations

import json
import logging
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
#: A node's namespace is `namespace`, falling back to `group_id`, falling back to 'default'.
_NAMESPACE_EXPR = "coalesce(n.namespace, n.group_id, 'default')"


class ExportRefused(Exception):
    """Raised when the export cannot prove it is safe to read. Never a partial bundle."""


@dataclass
class ExportCoverage:
    """What the bundle contains, and -- more importantly -- what it does not."""

    unaddressable_rows: dict[str, int] = field(default_factory=dict)
    unaddressable_total: int = 0
    suppressed_namespaces: list[str] = field(default_factory=list)
    excluded_properties: list[str] = field(default_factory=list)
    namespace_filter: str | None = None
    notes: list[str] = field(default_factory=list)


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean(properties: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in properties.items() if k not in EXCLUDED_PROPERTIES}


def _write_jsonl(path: Path, rows) -> int:
    """Stream rows to JSONL. Returns the count actually written."""
    written = 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, default=str, ensure_ascii=False) + "\n")
            written += 1
    return written


def _assert_readable_suppression_state(telemetry_db_path: Path, namespace: str | None) -> list[str]:
    """Prove the suppression state is readable, and return namespaces under a live erasure.

    Fails CLOSED. `suppressed_node_uuids` already suppresses every candidate when it cannot read
    the store, which is right for a paged reader that can still return a smaller page. An export
    claims completeness, so degrading to "exported everything except an unknown set" would be a
    false claim -- it refuses instead.
    """
    from menhir.infrastructure.erasure_subjects import ErasureSubjectStore

    try:
        store = ErasureSubjectStore(telemetry_db_path)
        if namespace is not None:
            return [namespace] if store.has_live_erasure(
                subject_type="NAMESPACE", subject_value=namespace
            ) else []
        # Whole-store export: any live erasure at all makes an unqualified "everything" claim
        # false, so enumerate them rather than assert completeness over an unknown exclusion.
        return _live_erasure_namespaces(store)
    except Exception as exc:  # noqa: BLE001 - refusing is the point; the cause is reported
        raise ExportRefused(
            "cannot read erasure suppression state, so this export cannot prove it would not "
            f"include content a pending erasure is withholding: {exc}"
        ) from exc


def _live_erasure_namespaces(store) -> list[str]:
    from menhir.infrastructure.telemetry import connect_telemetry_db

    with connect_telemetry_db(store.db_path) as conn:
        rows = conn.execute(
            "SELECT DISTINCT subject_value FROM erasure_subjects "
            "WHERE subject_type = 'NAMESPACE' AND purged_at IS NULL"
        ).fetchall()
    return sorted(str(r[0]) for r in rows if r and r[0])


def _suppressed_uuids(telemetry_db_path: Path, candidates: list[str]) -> frozenset[str]:
    from menhir.infrastructure.erasure_subjects import suppressed_node_uuids

    if not candidates:
        return frozenset()
    return suppressed_node_uuids(telemetry_db_path, candidates)


def export_subject_data(
    neo4j,
    *,
    output_dir: Path | str,
    telemetry_db_path: Path | str | None = None,
    namespace: str | None = None,
    database: str | None = None,
) -> dict[str, Any]:
    """Write a bundle of everything menhir holds. Read-only; returns the manifest.

    Args:
        neo4j: a `Neo4jRepository`-shaped object exposing `execute(query, params)`.
        output_dir: directory the bundle is written to. Created if absent.
        telemetry_db_path: sidecar path. Defaults to the configured telemetry DB.
        namespace: optional project-scope filter. NOT a subject boundary; see the module docstring.
        database: unused placeholder kept out of the query path deliberately -- the repository owns
            its own database binding, and accepting one here would let a caller export from a
            different database than the one the suppression state describes.
    """
    del database

    from menhir.infrastructure.telemetry import connect_telemetry_db, default_telemetry_db_path

    out = Path(output_dir)
    db_path = Path(telemetry_db_path) if telemetry_db_path else Path(default_telemetry_db_path())

    coverage = ExportCoverage(
        excluded_properties=sorted(EXCLUDED_PROPERTIES), namespace_filter=namespace
    )

    # (1) Suppression first. Nothing is read from the graph until this holds.
    coverage.suppressed_namespaces = _assert_readable_suppression_state(db_path, namespace)
    if namespace is not None and coverage.suppressed_namespaces:
        raise ExportRefused(
            f"namespace {namespace!r} is under a live, unpurged erasure; exporting it would serve "
            "content that erasure is withholding"
        )

    ns_clause = f"WHERE {_NAMESPACE_EXPR} = $namespace" if namespace else ""
    params = {"namespace": namespace} if namespace else {}

    # (2) Graph nodes. Every label, whole. `labels(n)` is carried on each row so a consumer can
    #     filter, and structural rows are counted separately in the manifest rather than dropped --
    #     silently omitting them would be exactly the kind of unstated gap this export must not have.
    node_rows = neo4j.execute(
        f"""
        MATCH (n)
        {ns_clause}
        RETURN coalesce(n.uuid, '') AS uuid, labels(n) AS labels,
               {_NAMESPACE_EXPR} AS namespace,
               n.structure_role IS NOT NULL AS structural,
               properties(n) AS properties
        """,
        params,
    )
    candidate_uuids = [str(r["uuid"]) for r in node_rows if r.get("uuid")]
    suppressed = _suppressed_uuids(db_path, candidate_uuids)

    kept_nodes = [r for r in node_rows if str(r.get("uuid") or "") not in suppressed]
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

    # (3) Relationships, whole -- topology included, so the bundle reconstructs the graph and not
    #     just its text. Suppressed endpoints drop the edge with them.
    rel_rows = neo4j.execute(
        f"""
        MATCH (n)-[r]->(m)
        {ns_clause}
        RETURN type(r) AS type, coalesce(n.uuid, '') AS start_uuid,
               coalesce(m.uuid, '') AS end_uuid, properties(r) AS properties
        """,
        params,
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
            if str(r.get("start_uuid") or "") not in suppressed
            and str(r.get("end_uuid") or "") not in suppressed
        ),
    )

    # (4) Sidecar, driven by the erasure registry so a new content column joins the export the
    #     moment it is classified -- the registry's guarding test already fails on an unclassified
    #     TEXT column, so the export inherits that ratchet instead of keeping a second list.
    sidecar_counts, unaddressable = _export_sidecar(db_path, out, namespace, connect_telemetry_db)
    coverage.unaddressable_rows = unaddressable
    coverage.unaddressable_total = sum(unaddressable.values())

    coverage.notes = [
        "Embeddings are excluded: derived vectors, recomputable from the exported text.",
        "unaddressable_rows counts content rows NO subject key can reach (CF-165); they are "
        "operational telemetry, and no unaddressable row exceeds 1,000 characters.",
        "structural_nodes are code-structure index rows from the project scanner, not authored "
        "content. They are included and counted, never silently dropped.",
    ]
    if namespace is not None:
        coverage.notes.append(
            "NAMESPACE-FILTERED BUNDLE: a project scope, not a subject boundary. This is not a "
            "complete record of everything menhir holds about the subject."
        )
    if coverage.suppressed_namespaces:
        coverage.notes.append(
            "Namespaces under a live unpurged erasure were excluded: "
            + ", ".join(coverage.suppressed_namespaces)
        )
    if suppressed:
        coverage.notes.append(
            f"{len(suppressed)} node(s) withheld: covered by a committed but unpurged erasure."
        )

    manifest = {
        "created_at": _utc_stamp(),
        "graph_nodes": node_count,
        "graph_nodes_structural": structural_count,
        "graph_relationships": rel_count,
        "sidecar_rows": sidecar_counts,
        "sidecar_rows_total": sum(sidecar_counts.values()),
        "withheld_node_uuids": len(suppressed),
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
    db_path: Path, out: Path, namespace: str | None, connect
) -> tuple[dict[str, int], dict[str, int]]:
    """Export every classified content column, one JSONL per table.

    Rows are emitted whole (all columns), not just the content column: a content value without the
    row that carried it is not a record of anything. The registry decides WHICH tables hold content;
    it does not narrow what a row is.
    """
    from menhir.infrastructure.telemetry.erasure_inventory import CONTENT_COLUMNS
    from menhir.infrastructure.telemetry.erasure_purge import count_unaddressable_content

    tables: dict[str, set[str]] = {}
    for entry in CONTENT_COLUMNS:
        tables.setdefault(entry.table, set()).update(entry.key_columns)

    counts: dict[str, int] = {}
    with connect(db_path) as conn:
        conn.row_factory = __import__("sqlite3").Row
        unaddressable = count_unaddressable_content(conn)
        for table, key_columns in sorted(tables.items()):
            if not _table_exists(conn, table):
                continue
            where, params = "", []
            # A namespace filter applies only where the table actually carries a namespace key;
            # narrowing on a column that does not exist would silently export nothing.
            if namespace is not None and "namespace" in key_columns:
                if _column_exists(conn, table, "namespace"):
                    where, params = " WHERE namespace = ?", [namespace]
            rows = conn.execute(f"SELECT * FROM {table}{where}", params)  # noqa: S608 - registry name
            counts[table] = _write_jsonl(
                out / f"sidecar_{table}.jsonl", (dict(r) for r in rows)
            )
    return counts, unaddressable


def _table_exists(conn, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1", (table,)
    ).fetchone()
    return row is not None


def _column_exists(conn, table: str, column: str) -> bool:
    return any(r[1] == column for r in conn.execute(f"PRAGMA table_info({table})"))
