"""Read-only logical backup of the memory graph to a gzipped JSONL file.

This exists because there was no backup of the production graph before destructive
lifecycle work. It connects to the SAME database the menhir server uses
(MemorySettings.from_env(), i.e. the real prod), reads every node and relationship,
and writes them to a timestamped file. It issues ONLY MATCH ... RETURN queries -- it
never writes to the graph.

Format (gzipped JSON lines):
    {"t":"meta", ...}                                     one header line
    {"t":"node","eid":..,"labels":[..],"props":{..}}      one per node
    {"t":"rel","start":..,"end":..,"type":..,"props":{..}} one per relationship
Relationship endpoints reference node ``eid`` (Neo4j elementId), so the dump is
self-consistent and restorable by a straightforward importer.

Embeddings (`*embedding*` properties) are EXCLUDED by default: they are large and
re-derivable from content. Pass --include-embeddings for a byte-complete dump.

Usage:
    python scripts/export_graph_backup.py                 # backs up prod (from .env)
    python scripts/export_graph_backup.py --out PATH
    python scripts/export_graph_backup.py --include-embeddings
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from menhir.config import MemorySettings
from menhir.infrastructure.neo4j import Neo4jRepository

_BATCH = 5000


def _strip_embeddings(props: dict) -> dict:
    return {k: v for k, v in props.items() if "embedding" not in k.lower()}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", help="Output path (default: backups/prod-neo4j-export-<ts>.jsonl.gz)")
    ap.add_argument("--include-embeddings", action="store_true", help="Keep embedding properties")
    ap.add_argument("--uri", help="Override Neo4j URI (default: from .env / MemorySettings)")
    args = ap.parse_args()

    load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)
    s = MemorySettings.from_env()
    uri = args.uri or s.neo4j_uri
    repo = Neo4jRepository(uri=uri, database=s.neo4j_database, user=s.neo4j_user, password=s.neo4j_password)

    keep = (lambda p: p) if args.include_embeddings else _strip_embeddings

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    if args.out:
        out = Path(args.out)
    else:
        out = Path(os.getenv("MENHIR_BACKUP_DIR") or Path(__file__).resolve().parents[1] / "backups") / f"prod-neo4j-export-{ts}.jsonl.gz"
    out.parent.mkdir(parents=True, exist_ok=True)

    try:
        node_total = repo.execute("MATCH (n) RETURN count(n) AS c")[0]["c"]
        rel_total = repo.execute("MATCH ()-[r]->() RETURN count(r) AS c")[0]["c"]
    except Exception as exc:  # noqa: BLE001
        print(f"FAILED to reach graph at {uri}: {exc}", file=sys.stderr)
        repo.close()
        return 2

    print(f"Backing up {uri}  (db={s.neo4j_database})")
    print(f"  nodes={node_total}  relationships={rel_total}")
    print(f"  -> {out}")
    print(f"  embeddings: {'included' if args.include_embeddings else 'excluded (re-derivable)'}")

    nodes_written = rels_written = 0
    try:
        with gzip.open(out, "wt", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "t": "meta", "source_uri": uri, "database": s.neo4j_database,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "node_total": node_total, "rel_total": rel_total,
                "embeddings_included": args.include_embeddings,
            }) + "\n")

            skip = 0
            while skip < node_total:
                rows = repo.execute(
                    "MATCH (n) RETURN elementId(n) AS eid, labels(n) AS labels, properties(n) AS props "
                    "ORDER BY elementId(n) SKIP $skip LIMIT $limit",
                    params={"skip": skip, "limit": _BATCH},
                )
                if not rows:
                    break
                for r in rows:
                    fh.write(json.dumps({
                        "t": "node", "eid": r["eid"], "labels": r["labels"],
                        "props": keep(r["props"] or {}),
                    }, default=str) + "\n")
                nodes_written += len(rows)
                skip += len(rows)
                print(f"  nodes {nodes_written}/{node_total}", end="\r", flush=True)
            print()

            skip = 0
            while skip < rel_total:
                rows = repo.execute(
                    "MATCH (a)-[r]->(b) RETURN elementId(a) AS start, elementId(b) AS end, "
                    "type(r) AS type, properties(r) AS props "
                    "ORDER BY elementId(r) SKIP $skip LIMIT $limit",
                    params={"skip": skip, "limit": _BATCH},
                )
                if not rows:
                    break
                for r in rows:
                    fh.write(json.dumps({
                        "t": "rel", "start": r["start"], "end": r["end"],
                        "type": r["type"], "props": keep(r["props"] or {}),
                    }, default=str) + "\n")
                rels_written += len(rows)
                skip += len(rows)
                print(f"  rels {rels_written}/{rel_total}", end="\r", flush=True)
            print()
    finally:
        repo.close()

    size_mb = out.stat().st_size / (1024 * 1024)
    print(f"DONE. nodes={nodes_written} rels={rels_written}  file={size_mb:.1f} MB")
    if nodes_written != node_total or rels_written != rel_total:
        print(f"WARNING: wrote {nodes_written}/{node_total} nodes, {rels_written}/{rel_total} rels "
              "(graph changed mid-export or pagination gap)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
