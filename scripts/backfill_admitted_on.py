"""Backfill the `(:Episodic)-[:ADMITTED_ON]->(:TurnEvidence)` admission join on an existing graph.

WHY THIS EXISTS
`ADMITTED_ON` is written at ingest from the admission verdict (services/ingest_intake.py), so it is
forward-fill only: every graph built before that landed has zero edges. Typed-scalar binding now
resolves a `turn_id` to its episode through this edge (infrastructure/episode_lifecycle.py,
`fetch_linked_entities_for_episode`), so on a pre-existing corpus non-self subjects still cannot
bind until the join is backfilled.

THE JOIN IS EXACT, NOT FUZZY
A `:TurnEvidence` carries the raw user text; the corresponding `:Episodic` stores it prefixed with
the producer's role marker. The join is therefore exact string equality on
`e.content = 'user: ' + t.text` within one namespace -- NOT similarity, NOT CONTAINS. Anything
looser risks attaching an assertion's provenance to the wrong turn, which is worse than leaving it
unbound: a wrong binding asserts authority the evidence never supported.

AMBIGUITY IS EXPECTED AND IS NOT AN ERROR
Extraction-collapse retries can leave duplicate episodes with byte-identical content, so one turn
can match several episodes. Those are genuine duplicates of the same turn, so any of them is a
correct provenance target; the tie is broken deterministically on (created_at, uuid) so repeated
runs converge on the same edge rather than flapping. Ambiguous turns are counted and reported
separately -- a rising count means duplicate episodes are accumulating, which is its own defect.

Read-only unless --apply is passed.

Usage:
  python scripts/backfill_admitted_on.py bolt://localhost:7704 --password lmedata123
  python scripts/backfill_admitted_on.py bolt://localhost:7704 --password lmedata123 --apply
  python scripts/backfill_admitted_on.py ... --namespace-prefix lme-

Writes to any non-loopback target require --allow-remote. A target matching
MENHIR_PROD_MARKERS requires --allow-production, which should be used only after a
verified backup.
"""

from __future__ import annotations

import argparse
import os
import sys

from neo4j import GraphDatabase

from menhir.infrastructure.script_safety import write_refusal_reason

# Fail closed before remote writes: a mistyped URI on an operator's terminal is exactly
# how a dry-run-first backfill becomes an incident when --apply is added.
#
# Set MENHIR_PROD_MARKERS to a comma-separated list of substrings identifying your own
# production host (hostname or address). Any target URI containing one is refused. The
# default catches a host literally named "menhir-prod"; every other remote target still
# requires --allow-remote even when no marker matches.
_PROD_MARKERS = tuple(
    m.strip()
    for m in os.getenv("MENHIR_PROD_MARKERS", "menhir-prod").split(",")
    if m.strip()
)

_SURVEY = """
MATCH (t:TurnEvidence)
WHERE $prefix IS NULL OR t.namespace STARTS WITH $prefix
OPTIONAL MATCH (e:Episodic)
  WHERE e.group_id = t.namespace AND e.content = 'user: ' + t.text
WITH t, count(e) AS matches
OPTIONAL MATCH (t)<-[r:ADMITTED_ON]-(:Episodic)
WITH t, matches, count(r) AS existing
RETURN count(t) AS turns,
       sum(CASE WHEN existing > 0 THEN 1 ELSE 0 END) AS already_linked,
       sum(CASE WHEN existing = 0 AND matches = 1 THEN 1 ELSE 0 END) AS linkable,
       sum(CASE WHEN existing = 0 AND matches > 1 THEN 1 ELSE 0 END) AS ambiguous,
       sum(CASE WHEN existing = 0 AND matches = 0 THEN 1 ELSE 0 END) AS unmatched
"""

# Deterministic tie-break lives in the ORDER BY: identical-content duplicates are all correct
# targets, so pick the oldest and keep re-runs idempotent.
_APPLY = """
MATCH (t:TurnEvidence)
WHERE ($prefix IS NULL OR t.namespace STARTS WITH $prefix)
  AND NOT EXISTS { (t)<-[:ADMITTED_ON]-(:Episodic) }
MATCH (e:Episodic)
WHERE e.group_id = t.namespace AND e.content = 'user: ' + t.text
WITH t, e ORDER BY e.created_at, e.uuid
WITH t, head(collect(e)) AS chosen
MERGE (chosen)-[:ADMITTED_ON]->(t)
RETURN count(*) AS linked
"""

_UNMATCHED_SAMPLE = """
MATCH (t:TurnEvidence)
WHERE ($prefix IS NULL OR t.namespace STARTS WITH $prefix)
  AND NOT EXISTS { (t)<-[:ADMITTED_ON]-(:Episodic) }
  AND NOT EXISTS { MATCH (e:Episodic)
                   WHERE e.group_id = t.namespace AND e.content = 'user: ' + t.text }
RETURN t.namespace AS ns, t.turn_id AS turn_id, substring(coalesce(t.text, ''), 0, 70) AS text
LIMIT 10
"""


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("uri", help="bolt URI, e.g. bolt://localhost:7704")
    p.add_argument("--password", required=True)
    p.add_argument("--user", default="neo4j")
    p.add_argument("--namespace-prefix", default=None,
                   help="restrict to namespaces with this prefix (default: all)")
    p.add_argument("--apply", action="store_true", help="write the edges (default: survey only)")
    p.add_argument("--allow-remote", action="store_true",
                   help="acknowledge a write to a non-loopback target")
    p.add_argument("--allow-production", action="store_true",
                   help="acknowledge a write to a configured production target after taking a backup")
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

    prefix = args.namespace_prefix
    driver = GraphDatabase.driver(args.uri, auth=(args.user, args.password))
    try:
        with driver.session(database="neo4j") as s:
            before = s.run(_SURVEY, prefix=prefix).single()
            print(f"== survey ({args.uri}, prefix={prefix!r}) ==")
            print(f"   TurnEvidence total : {before['turns']}")
            print(f"   already linked     : {before['already_linked']}")
            print(f"   linkable (1 match) : {before['linkable']}")
            print(f"   ambiguous (>1)     : {before['ambiguous']}  (duplicate episodes; oldest wins)")
            print(f"   unmatched (0)      : {before['unmatched']}")

            if before["unmatched"]:
                print("\n   -- sample of unmatched turns (no episode with exactly this content) --")
                for r in s.run(_UNMATCHED_SAMPLE, prefix=prefix).data():
                    print(f"      [{r['ns']}] {r['turn_id'][:12]} {r['text']!r}")

            if not args.apply:
                writable = (before["linkable"] or 0) + (before["ambiguous"] or 0)
                print(f"\nDRY RUN -- would create {writable} edges. Re-run with --apply to write.")
                return 0

            linked = s.run(_APPLY, prefix=prefix).single()["linked"]
            after = s.run(_SURVEY, prefix=prefix).single()
            print(f"\n== applied ==\n   edges created      : {linked}")
            print(f"   now linked         : {after['already_linked']} / {after['turns']}")
            print(f"   still unmatched    : {after['unmatched']}")
    finally:
        driver.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
