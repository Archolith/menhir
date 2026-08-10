"""Migrate the schema-version marker from `_yawn_schema_v` to `_menhir_schema_v`.

WHY THIS EXISTS
The property was renamed as part of removing another product's name from Menhir's
storage contract. Renaming it in code alone is not sufficient on an existing graph,
for two reasons:

1. The old indexes are named `<label>_schema_v_idx` and the new ones
   `<label>_menhir_schema_v_idx`. The name is not derived from the property, and the
   creates use IF NOT EXISTS, so the old index would survive as a live index over a
   property nothing writes any more -- dead weight on every write to those labels.
2. Every node and edge keeps a stale `_yawn_schema_v` value forever.

WHAT IT DOES NOT NEED TO DO
It does not need to copy values across. The bootstrap defaults queries match on
`_menhir_schema_v IS NULL OR < _SCHEMA_V`, and every SET in them is guarded by
coalesce() or a CASE, so re-running against already-populated nodes changes nothing
except stamping the new marker. Copying the old value would actually be worse: it
would suppress that re-stamp on nodes whose defaults predate the current _SCHEMA_V.

ORDER MATTERS
Drop the old indexes BEFORE removing the property. Removing an indexed property from
every node with the index still live forces Neo4j to maintain that index through the
whole rewrite for nothing.

Read-only unless --apply is passed.

Usage:
  python scripts/migrate_schema_v_property.py bolt://localhost:7687 --password pw
  python scripts/migrate_schema_v_property.py bolt://localhost:7687 --password pw --apply
  python scripts/migrate_schema_v_property.py bolt://remote:7687 --password pw --apply --allow-remote
  python scripts/migrate_schema_v_property.py bolt://prod:7687 --password pw --apply --allow-production
"""

from __future__ import annotations

import argparse
import os
import sys

from neo4j import GraphDatabase

from menhir.infrastructure.script_safety import write_refusal_reason

# Same guard as backfill_admitted_on.py: refuse a host the operator marks as production.
_PROD_MARKERS = tuple(
    m.strip()
    for m in os.getenv("MENHIR_PROD_MARKERS", "menhir-prod").split(",")
    if m.strip()
)

_SURVEY_NODES = """
MATCH (n)
WHERE n._yawn_schema_v IS NOT NULL
RETURN count(n) AS c
"""

_SURVEY_EDGES = """
MATCH ()-[r]-()
WHERE r._yawn_schema_v IS NOT NULL
RETURN count(DISTINCT r) AS c
"""

# Batched so a large graph does not run as one transaction.
_STRIP_NODES = """
MATCH (n)
WHERE n._yawn_schema_v IS NOT NULL
CALL (n) {
  REMOVE n._yawn_schema_v
} IN TRANSACTIONS OF 10000 ROWS
"""

_STRIP_EDGES = """
MATCH ()-[r]-()
WHERE r._yawn_schema_v IS NOT NULL
CALL (r) {
  REMOVE r._yawn_schema_v
} IN TRANSACTIONS OF 10000 ROWS
"""


def _new_schema_v_index_queries() -> list[str]:
    """The CREATE INDEX statements for the renamed property, taken from the schema module.

    Read out of `get_phase1_bootstrap_queries()` by matching on the property rather than
    hardcoded here, so this script cannot drift from the labels the schema actually
    declares. All statements are IF NOT EXISTS, so re-running is safe.
    """
    from menhir.infrastructure.schema import get_phase1_bootstrap_queries

    return [
        q.strip()
        for q in get_phase1_bootstrap_queries()
        if "CREATE INDEX" in q and "_menhir_schema_v" in q
    ]


def _old_schema_v_indexes(session) -> list[str]:
    """Names of surviving indexes over the old property.

    Matched on the indexed property, not on the name, so an index created under an
    older naming convention is still found.
    """
    rows = session.run(
        "SHOW INDEXES YIELD name, properties "
        "WHERE '_yawn_schema_v' IN properties "
        "RETURN name"
    ).data()
    return [r["name"] for r in rows]


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("uri", help="bolt URI")
    p.add_argument("--password", required=True)
    p.add_argument("--user", default="neo4j")
    p.add_argument("--database", default="neo4j")
    p.add_argument("--apply", action="store_true", help="perform the migration")
    p.add_argument(
        "--allow-remote",
        action="store_true",
        help="acknowledge a write to a non-loopback target",
    )
    p.add_argument(
        "--allow-production",
        action="store_true",
        help="acknowledge a write to a configured production target after taking a backup",
    )
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    if args.apply:
        try:
            refusal = write_refusal_reason(
                args.uri,
                production_markers=_PROD_MARKERS,
                allow_remote=args.allow_remote,
                allow_production=args.allow_production,
            )
        except ValueError as exc:
            print(f"REFUSING: {exc}", file=sys.stderr)
            return 2
        if refusal:
            print(f"REFUSING: {refusal}.", file=sys.stderr)
            return 2

    driver = GraphDatabase.driver(args.uri, auth=(args.user, args.password))
    try:
        with driver.session(database=args.database) as s:
            nodes = s.run(_SURVEY_NODES).single()["c"]
            edges = s.run(_SURVEY_EDGES).single()["c"]
            stale_indexes = _old_schema_v_indexes(s)

            print(f"== survey ({args.uri}) ==")
            print(f"   nodes carrying _yawn_schema_v : {nodes}")
            print(f"   edges carrying _yawn_schema_v : {edges}")
            print(f"   indexes over old property     : {len(stale_indexes)}")
            for n in stale_indexes:
                print(f"      - {n}")

            if not args.apply:
                print("\nDRY RUN -- re-run with --apply to migrate.")
                return 0

            for name in stale_indexes:
                escaped_name = name.replace("`", "``")
                s.run(f"DROP INDEX `{escaped_name}` IF EXISTS")
            print(f"\n   dropped indexes : {len(stale_indexes)}")

            # Create the replacements here rather than leaving it to server startup.
            # bootstrap_phase_one() only runs when phase_one_schema_ready() is False, and
            # that gate checks PHASE_ONE_REQUIRED_INDEXES, which does not list the
            # schema-version indexes. On any established graph the gate passes, bootstrap
            # is skipped, and the dropped indexes would never come back -- leaving prod
            # permanently short of the indexes a fresh install creates.
            created = 0
            for query in _new_schema_v_index_queries():
                s.run(query)
                created += 1
            s.run("CALL db.awaitIndexes(300)")
            print(f"   created indexes : {created}")

            s.run(_STRIP_NODES)
            s.run(_STRIP_EDGES)

            after_nodes = s.run(_SURVEY_NODES).single()["c"]
            after_edges = s.run(_SURVEY_EDGES).single()["c"]
            print(f"   nodes remaining : {after_nodes}")
            print(f"   edges remaining : {after_edges}")
            if after_nodes or after_edges:
                print("WARNING: property still present somewhere.", file=sys.stderr)
                return 1

            replacement_states = s.run(
                "SHOW INDEXES YIELD name, properties, state "
                "WHERE '_menhir_schema_v' IN properties "
                "RETURN name, state ORDER BY name"
            ).data()
            expected_indexes = len(_new_schema_v_index_queries())
            online_indexes = sum(row["state"] == "ONLINE" for row in replacement_states)
            print(f"   replacement indexes online : {online_indexes}/{expected_indexes}")
            if len(replacement_states) != expected_indexes or online_indexes != expected_indexes:
                print("WARNING: replacement index verification failed.", file=sys.stderr)
                return 1

            print("\nDone. No restart is required: the replacement indexes are created")
            print("above. _menhir_schema_v is left unset, which is correct -- it is a")
            print("backfill marker, and the defaults queries that stamp it only run when")
            print("bootstrap runs. On an established graph bootstrap is skipped, and the")
            print("old marker was equally vestigial there. If bootstrap ever does run, it")
            print("matches every node and re-applies coalesce-guarded defaults, which is")
            print("a no-op on populated nodes.")
    finally:
        driver.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
