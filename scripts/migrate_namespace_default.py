"""One-shot migration: normalize legacy nodes into the default namespace silo.

Background: the memory namespace feature maps a menhir namespace onto graphiti's
``group_id``. ``DEFAULT_NAMESPACE`` ("default") maps to graphiti group "". Most legacy
data already has ``group_id = ""``, but some nodes have ``group_id = NULL`` (and no
``namespace`` property). A scoped ``recall(namespace="default")`` filters on group "" and
would miss the NULL-group nodes. This migration fills both properties so all legacy data
is uniformly addressable as the default silo. It is additive and idempotent (no deletes).

Dry-run by default; pass ``--apply`` to write.

    python scripts/migrate_namespace_default.py            # report only
    python scripts/migrate_namespace_default.py --apply    # perform the migration
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from dotenv import load_dotenv

from menhir.config import MemorySettings
from menhir.infrastructure.neo4j import Neo4jRepository


def _load_settings() -> MemorySettings:
    repo_root = Path(__file__).resolve().parents[1]
    load_dotenv(os.getenv("ENV_FILE") or repo_root / ".env")
    return MemorySettings.from_env()


def _count(neo4j: Neo4jRepository, predicate: str) -> int:
    rows = neo4j.execute(f"MATCH (n) WHERE {predicate} RETURN count(n) AS c")
    return int(rows[0]["c"]) if rows else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Perform the migration (default: dry run).")
    args = parser.parse_args()

    settings = _load_settings()
    neo4j = Neo4jRepository(
        uri=settings.neo4j_uri,
        database=settings.neo4j_database,
        user=settings.neo4j_user,
        password=settings.neo4j_password,
    )

    null_group = _count(neo4j, "n.group_id IS NULL")
    null_namespace = _count(neo4j, "n.namespace IS NULL")
    print(f"[before] nodes with group_id IS NULL : {null_group}")
    print(f"[before] nodes with namespace IS NULL: {null_namespace}")

    if not args.apply:
        print("[dry-run] no changes written. Re-run with --apply to migrate.")
        return 0

    g = neo4j.execute(
        "MATCH (n) WHERE n.group_id IS NULL SET n.group_id = '' RETURN count(n) AS c"
    )
    ns = neo4j.execute(
        "MATCH (n) WHERE n.namespace IS NULL SET n.namespace = 'default' RETURN count(n) AS c"
    )
    print(f"[apply] set group_id='' on {int(g[0]['c']) if g else 0} nodes")
    print(f"[apply] set namespace='default' on {int(ns[0]['c']) if ns else 0} nodes")

    after_group = _count(neo4j, "n.group_id IS NULL")
    after_namespace = _count(neo4j, "n.namespace IS NULL")
    print(f"[after] nodes with group_id IS NULL : {after_group}")
    print(f"[after] nodes with namespace IS NULL: {after_namespace}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
