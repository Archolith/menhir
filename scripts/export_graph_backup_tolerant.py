"""Corruption-tolerant logical backup of the memory graph to a gzipped JSONL file.

This is the fault-tolerant sibling of ``export_graph_backup.py``. Use that one on a healthy
graph; use this one when the store has record-level corruption.

WHY THIS EXISTS
    ``export_graph_backup.py`` pages ``properties(n)`` 5000 rows at a time. A single unreadable
    property record (e.g. the dangling property-key token found on prod 2026-07-25, which raises
    ``Property key with id=2137 not found``) fails the whole 5000-row batch, and the export dies.
    With that corruption present there is NO working logical backup path -- which is exactly when
    a backup matters most. This script degrades instead of failing: it isolates unreadable
    entities, skips them, and exports everything else.

STRATEGY
    Read a window of rows. If the read raises, split the window in half and retry each half,
    recursing until the failure is isolated to a single entity. That entity is recorded (with
    whatever identity can be read WITHOUT touching its property chain -- id, elementId, labels,
    relationship type) and skipped. Cost is O(batch) when clean and O(log batch) extra reads per
    corrupt entity, so a healthy graph pays essentially nothing.

READ-ONLY
    Issues only MATCH ... RETURN. It never writes to the graph.

Format (gzipped JSON lines) -- a superset of export_graph_backup.py's format:
    {"t":"meta", ...}                                       one header line
    {"t":"node","eid":..,"labels":[..],"props":{..}}        one per readable node
    {"t":"rel","start":..,"end":..,"type":..,"props":{..}}  one per readable relationship
    {"t":"skip_node","offset":..,"id":..,"labels":[..],"error":".."}   one per unreadable node
    {"t":"skip_rel","offset":..,"id":..,"type":"..","error":".."}      one per unreadable rel
    {"t":"summary", ...}                                    one trailer line
Consumers of the original format should ignore unknown ``t`` values.

Exit codes:
    0  complete backup, nothing skipped
    1  partial backup, one or more entities unreadable (see skip_* lines and stderr summary)
    2  could not reach the graph at all

Usage:
    python scripts/export_graph_backup_tolerant.py
    python scripts/export_graph_backup_tolerant.py --out PATH --batch 500
    python scripts/export_graph_backup_tolerant.py --include-embeddings
"""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from menhir.config import MemorySettings
from menhir.infrastructure.neo4j import Neo4jRepository

# The paging queries use id() for a stable numeric SKIP/LIMIT ordering, which the server flags as
# deprecated on EVERY batch. At ~170 batches that notification noise buries the skip_* lines this
# script exists to surface, so silence the notification logger (not the errors) here.
logging.getLogger("neo4j.notifications").setLevel(logging.ERROR)

_BATCH = 1000

# Identity projections that do NOT touch the property chain, so they still work on an entity
# whose properties are unreadable. labels()/type() come from the node/relationship record itself.
_NODE_ID_Q = (
    "MATCH (n) RETURN id(n) AS id, elementId(n) AS eid, labels(n) AS labels "
    "ORDER BY id(n) SKIP $skip LIMIT $limit"
)
_NODE_FULL_Q = (
    "MATCH (n) RETURN id(n) AS id, elementId(n) AS eid, labels(n) AS labels, "
    "properties(n) AS props ORDER BY id(n) SKIP $skip LIMIT $limit"
)
_REL_ID_Q = (
    "MATCH ()-[r]->() RETURN id(r) AS id, type(r) AS type "
    "ORDER BY id(r) SKIP $skip LIMIT $limit"
)
_REL_FULL_Q = (
    "MATCH (a)-[r]->(b) RETURN id(r) AS id, elementId(a) AS start, elementId(b) AS end, "
    "type(r) AS type, properties(r) AS props ORDER BY id(r) SKIP $skip LIMIT $limit"
)


def _strip_embeddings(props: dict) -> dict:
    return {k: v for k, v in props.items() if "embedding" not in k.lower()}


def _identify(repo: Neo4jRepository, id_query: str, offset: int) -> dict:
    """Best-effort identity for an entity we could not read. Never raises."""
    try:
        rows = repo.execute(id_query, params={"skip": offset, "limit": 1})
        return dict(rows[0]) if rows else {}
    except Exception:  # noqa: BLE001 - identity is best-effort by definition
        return {}


def _read_window(
    repo: Neo4jRepository,
    full_query: str,
    id_query: str,
    skip: int,
    limit: int,
    emit,
    skipped: list,
    kind: str,
) -> int:
    """Read [skip, skip+limit); on failure halve until the bad entity is isolated.

    Returns the number of rows successfully emitted. Appends one dict per unreadable entity
    to ``skipped``.
    """
    try:
        rows = repo.execute(full_query, params={"skip": skip, "limit": limit})
    except Exception as exc:  # noqa: BLE001 - isolating the failure is the entire point
        if limit == 1:
            ident = _identify(repo, id_query, skip)
            rec = {
                "t": f"skip_{kind}",
                "offset": skip,
                "id": ident.get("id"),
                "error": f"{type(exc).__name__}: {str(exc)[:200]}",
            }
            if kind == "node":
                rec["labels"] = ident.get("labels")
            else:
                rec["type"] = ident.get("type")
            skipped.append(rec)
            emit(rec)
            return 0
        half = limit // 2
        n = _read_window(repo, full_query, id_query, skip, half, emit, skipped, kind)
        n += _read_window(
            repo, full_query, id_query, skip + half, limit - half, emit, skipped, kind
        )
        return n

    for r in rows:
        emit(r)
    return len(rows)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", help="Output path (default: backups/prod-neo4j-tolerant-<ts>.jsonl.gz)")
    ap.add_argument("--include-embeddings", action="store_true", help="Keep embedding properties")
    ap.add_argument("--uri", help="Override Neo4j URI (default: from .env / MemorySettings)")
    ap.add_argument("--batch", type=int, default=_BATCH, help=f"Window size (default {_BATCH})")
    args = ap.parse_args()

    s = MemorySettings.from_env()
    uri = args.uri or s.neo4j_uri
    repo = Neo4jRepository(
        uri=uri, database=s.neo4j_database, user=s.neo4j_user, password=s.neo4j_password
    )

    keep = (lambda p: p) if args.include_embeddings else _strip_embeddings

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out = Path(args.out) if args.out else (
        Path(os.getenv("MENHIR_BACKUP_DIR") or Path(__file__).resolve().parents[1] / "backups") / f"prod-neo4j-tolerant-{ts}.jsonl.gz"
    )
    out.parent.mkdir(parents=True, exist_ok=True)

    # count(n) does not read property chains, so it is safe on a corrupt store.
    try:
        node_total = repo.execute("MATCH (n) RETURN count(n) AS c")[0]["c"]
        rel_total = repo.execute("MATCH ()-[r]->() RETURN count(r) AS c")[0]["c"]
    except Exception as exc:  # noqa: BLE001
        print(f"FAILED to reach graph at {uri}: {exc}", file=sys.stderr)
        repo.close()
        return 2

    print(f"Backing up {uri}  (db={s.neo4j_database})  [TOLERANT MODE]")
    print(f"  nodes={node_total}  relationships={rel_total}  batch={args.batch}")
    print(f"  -> {out}")
    print(f"  embeddings: {'included' if args.include_embeddings else 'excluded (re-derivable)'}")

    skipped_nodes: list = []
    skipped_rels: list = []
    nodes_written = rels_written = 0

    try:
        with gzip.open(out, "wt", encoding="utf-8") as fh:
            def write(obj: dict) -> None:
                fh.write(json.dumps(obj, default=str) + "\n")

            write({
                "t": "meta", "source_uri": uri, "database": s.neo4j_database,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "node_total": node_total, "rel_total": rel_total,
                "embeddings_included": args.include_embeddings,
                "tolerant": True, "batch": args.batch,
            })

            def emit_node(r) -> None:
                if r.get("t"):          # a skip_* record, already shaped
                    write(r)
                    return
                write({
                    "t": "node", "eid": r["eid"], "labels": r["labels"],
                    "props": keep(r["props"] or {}),
                })

            def emit_rel(r) -> None:
                if r.get("t"):
                    write(r)
                    return
                write({
                    "t": "rel", "start": r["start"], "end": r["end"],
                    "type": r["type"], "props": keep(r["props"] or {}),
                })

            skip = 0
            while skip < node_total:
                limit = min(args.batch, node_total - skip)
                nodes_written += _read_window(
                    repo, _NODE_FULL_Q, _NODE_ID_Q, skip, limit, emit_node, skipped_nodes, "node"
                )
                skip += limit
                print(f"  nodes {nodes_written}/{node_total} (skipped {len(skipped_nodes)})",
                      end="\r", flush=True)
            print()

            skip = 0
            while skip < rel_total:
                limit = min(args.batch, rel_total - skip)
                rels_written += _read_window(
                    repo, _REL_FULL_Q, _REL_ID_Q, skip, limit, emit_rel, skipped_rels, "rel"
                )
                skip += limit
                print(f"  rels {rels_written}/{rel_total} (skipped {len(skipped_rels)})",
                      end="\r", flush=True)
            print()

            write({
                "t": "summary",
                "nodes_written": nodes_written, "nodes_skipped": len(skipped_nodes),
                "rels_written": rels_written, "rels_skipped": len(skipped_rels),
                "complete": not (skipped_nodes or skipped_rels),
            })
    finally:
        repo.close()

    size_mb = out.stat().st_size / (1024 * 1024)
    print(f"DONE. nodes={nodes_written} rels={rels_written}  file={size_mb:.1f} MB")

    if skipped_nodes or skipped_rels:
        print(
            f"\nPARTIAL BACKUP: {len(skipped_nodes)} node(s) and {len(skipped_rels)} "
            f"relationship(s) were unreadable and have been SKIPPED.",
            file=sys.stderr,
        )
        for rec in (skipped_nodes + skipped_rels)[:20]:
            print(f"  {rec['t']} id={rec.get('id')} offset={rec['offset']} "
                  f"{rec.get('labels') or rec.get('type')}: {rec['error']}", file=sys.stderr)
        if len(skipped_nodes) + len(skipped_rels) > 20:
            print(f"  ... and {len(skipped_nodes) + len(skipped_rels) - 20} more "
                  "(see skip_* lines in the archive)", file=sys.stderr)
        return 1

    # Pagination gaps are a different failure from corruption -- surface them separately.
    if nodes_written != node_total or rels_written != rel_total:
        print(f"WARNING: wrote {nodes_written}/{node_total} nodes, {rels_written}/{rel_total} "
              "rels with nothing skipped (graph changed mid-export or pagination gap)",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
