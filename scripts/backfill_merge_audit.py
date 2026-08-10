"""One-time backfill: copy graph merge_audit entries into the durable telemetry sidecar.

Merge history used to live ONLY as a ``merge_audit`` property on the survivor node.
That node is deletable (decay -> bridge_and_delete, orphan cleanup, user delete), so
every absorbed node's snapshot was one deletion away from being lost forever.

Merges now also write to the ``merge_audit`` table in the telemetry sidecar, which
outlives the graph node. This script backfills the entries recorded BEFORE that change
so existing history is protected too.

Idempotent: skips (survivor, absorbed) pairs already present in the sidecar.

Usage:
    python scripts/backfill_merge_audit.py [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import sys

from menhir.config import MemorySettings
from menhir.infrastructure.neo4j import Neo4jRepository
from menhir.infrastructure.telemetry import telemetry_store


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="Report what would be backfilled, write nothing"
    )
    args = parser.parse_args()

    settings = MemorySettings.from_env()
    repo = Neo4jRepository(
        uri=settings.neo4j_uri,
        database=settings.neo4j_database,
        user=settings.neo4j_user,
        password=settings.neo4j_password,
    )

    try:
        rows = repo.execute(
            """
            MATCH (n:Entity)
            WHERE n.merge_audit IS NOT NULL
            RETURN n.uuid AS survivor_uuid, n.merge_audit AS audit
            """
        )
    finally:
        repo.close()

    existing = {
        (r["survivor_uuid"], r["absorbed_uuid"])
        for r in telemetry_store.fetch_merge_audit(limit=100_000)
    }

    written = 0
    skipped = 0
    malformed = 0

    for row in rows:
        survivor = row["survivor_uuid"]
        for raw in row["audit"] or []:
            try:
                entry = json.loads(raw)
            except (TypeError, ValueError):
                malformed += 1
                continue

            absorbed = entry.get("absorbed_uuid")
            if not absorbed:
                malformed += 1
                continue

            if (survivor, absorbed) in existing:
                skipped += 1
                continue

            if args.dry_run:
                written += 1
                continue

            telemetry_store.record_merge(
                survivor_uuid=survivor,
                absorbed_uuid=absorbed,
                similarity=entry.get("similarity"),
                snapshot_json=raw,
            )
            existing.add((survivor, absorbed))
            written += 1

    verb = "would backfill" if args.dry_run else "backfilled"
    print(f"{verb}: {written}")
    print(f"already in sidecar (skipped): {skipped}")
    if malformed:
        print(f"malformed/unparseable entries: {malformed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
