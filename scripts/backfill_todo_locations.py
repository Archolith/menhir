"""Backfill :TodoLocation nodes from existing code_ref values.

Idempotent: locations at the current LOCATION_SCHEMA_VERSION are left alone, so
a re-run only touches todos that have none or were normalized by older rules.
Existing code_ref is never modified -- it remains the display value.

Usage:
    python scripts/backfill_todo_locations.py --dry-run
    python scripts/backfill_todo_locations.py --apply
"""

from __future__ import annotations

import argparse
import collections
import sys

from dotenv import load_dotenv

from menhir.config.settings_model import MemorySettings
from menhir.domain.todo_location import LOCATION_SCHEMA_VERSION, parse_code_ref
from menhir.infrastructure.neo4j import Neo4jRepository


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write locations (default: dry run)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    apply = bool(args.apply)

    load_dotenv()
    s = MemorySettings.from_env()
    n = Neo4jRepository(
        uri=s.neo4j_uri, user=s.neo4j_user, password=s.neo4j_password, database=s.neo4j_database
    )

    known = frozenset(
        str(r["p"])
        for r in n.execute(
            "MATCH (e:Entity) WHERE e.structure_project IS NOT NULL "
            "RETURN DISTINCT e.structure_project AS p",
            {},
        )
        if r.get("p")
    )

    todos = n.execute(
        """
        MATCH (t:Todo)
        WHERE t.code_ref IS NOT NULL
        OPTIONAL MATCH (t)-[:HAS_LOCATION]->(l:TodoLocation)
        WITH t, collect(l) AS locs
        RETURN t.uuid AS uuid, t.code_ref AS code_ref,
               [x IN locs WHERE x.schema_version = $ver] AS current
        """,
        {"ver": LOCATION_SCHEMA_VERSION},
    )

    stats = collections.Counter()
    reasons = collections.Counter()
    kinds = collections.Counter()
    planned: list[tuple[str, list[dict]]] = []

    for t in todos:
        stats["todos_with_code_ref"] += 1
        if t.get("current"):
            stats["skipped_already_current"] += 1
            continue

        locs = parse_code_ref(t["code_ref"], known_projects=known)
        if not locs:
            stats["no_locations_produced"] += 1
            continue

        planned.append((t["uuid"], [l.as_properties() for l in locs]))
        if len(locs) > 1:
            stats["multi_location_todos"] += 1
        for l in locs:
            stats[f"location_{l.resolution_status}"] += 1
            kinds[l.kind] += 1
            if l.unresolved_reason:
                reasons[l.unresolved_reason] += 1

    print("=== migration report ===")
    for k, v in sorted(stats.items()):
        print(f"  {k:<28} {v}")
    print(f"  {'locations_planned':<28} {sum(len(r) for _, r in planned)}")
    print("  kinds  :", dict(kinds))
    print("  reasons:", dict(reasons) or "{}")

    if not apply:
        print("\ndry run -- nothing written. Re-run with --apply.")
        return 0

    written = 0
    for uuid, rows in planned:
        # Replace any stale-version locations for this todo before writing.
        n.execute(
            "MATCH (t:Todo {uuid: $uuid})-[:HAS_LOCATION]->(l:TodoLocation) DETACH DELETE l",
            {"uuid": uuid},
        )
        n.execute(
            """
            MATCH (t:Todo {uuid: $uuid})
            UNWIND $rows AS row
            CREATE (t)-[:HAS_LOCATION]->(l:TodoLocation)
            SET l += row
            """,
            {"uuid": uuid, "rows": rows},
        )
        written += len(rows)

    print(f"\napplied: {written} locations across {len(planned)} todos")

    # Audit trail: raw declaration -> normalized location -> resolved entity.
    linked = n.execute(
        """
        MATCH (l:TodoLocation)
        WHERE l.resolution_status = 'resolved' AND l.path IS NOT NULL
        MATCH (f:Entity)
        WHERE f.structure_role IN ['file', 'entrypoint', 'config', 'test']
          AND (
                f.structure_path = l.path
                OR (NOT l.path CONTAINS '/' AND f.structure_path ENDS WITH ('/' + l.path))
              )
          AND (l.project IS NULL OR f.structure_project = l.project)
        MERGE (l)-[:REFERENCES_FILE]->(f)
        RETURN count(*) AS c
        """,
        {},
    )
    print(f"location -> file edges: {linked[0]['c'] if linked else 0}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
