"""Operator CLI for reversing a merge. Thin wrapper over UnmergeCoordinator -- it issues NO
restoration queries of its own (remediation plan Phase 5).

What changed and why
--------------------
This script used to perform the unmerge itself, in five separate statements guarded by "skip if the
node already exists". A crash after the first statement left a bare node with no edges, and every
rerun then SKIPPED it -- permanently half-restored, silently. Its snapshot was also lossy (a
10-property allowlist, Entity-only peers, one relationship property, stringified temporals), and it
could not reverse the survivor at all.

All of that is gone. An unmerge now runs as a journaled saga against the Phase 4 lossless snapshot:
one atomic restore, verified before it commits, and refused outright when it cannot be exact.

Two recovery tiers
------------------
* EXACT -- the merge was performed by the journaled coordinator (an ENTITY_MERGE row with a complete
  snapshot). Fully reversible: labels, typed properties, every relationship instance, provenance, and
  the survivor's pre-merge delta.
* DEGRADED -- the merge predates the journal, so only a lossy historical snapshot exists. It CANNOT
  be exactly reversed. The LEGACY_ENTITY_UNMERGE lane will partially restore it, but only from an
  explicit manifest, only with an acknowledgement, and it always reports the classes of state it
  could not bring back (labels, non-Entity peers, relationship properties, typed temporals, and the
  survivor's entire pre-merge state). It never claims an exact round trip.

Merges with lineage but NO snapshot are not recoverable by anything. `--inventory` says so plainly
rather than letting an operator discover it mid-incident.

Usage:
    python scripts/unmerge.py --inventory            # what can actually be recovered, by tier
    python scripts/unmerge.py --list                 # journaled merges reversible EXACTLY
    python scripts/unmerge.py --op <MERGE_OP_ID>     # dry run: show exactly what would be restored
    python scripts/unmerge.py --op <MERGE_OP_ID> --execute

    # DEGRADED recovery of pre-journal merges (the uuids are the manifest):
    python scripts/unmerge.py --legacy-absorbed <UUID> [<UUID> ...]
    python scripts/unmerge.py --legacy-absorbed <UUID> --execute
"""

from __future__ import annotations

import argparse
import json
import sys

from menhir.config import MemorySettings
from menhir.infrastructure.correlation_queries import CorrelationRepository
from menhir.infrastructure.graph_operations import GraphOperationsJournal
from menhir.infrastructure.neo4j import Neo4jRepository
from menhir.services.unmerge_coordinator import UnmergeCoordinator


def _build(settings: MemorySettings):
    repo = Neo4jRepository(
        uri=settings.neo4j_uri,
        user=settings.neo4j_user,
        password=settings.neo4j_password,
        database=settings.neo4j_database,
    )
    from menhir.services.legacy_unmerge_coordinator import LegacyUnmergeCoordinator

    journal = GraphOperationsJournal()
    correlation = CorrelationRepository(repo)
    return (
        UnmergeCoordinator(graph_adapter=correlation, journal=journal),
        LegacyUnmergeCoordinator(graph_adapter=correlation, journal=journal),
        journal,
        repo,
    )


def _list_reversible(journal: GraphOperationsJournal) -> int:
    rows = [
        r for r in journal.list_by_state("COMMITTED", limit=100_000)
        if r.get("operation_kind") == "ENTITY_MERGE" and r.get("before_snapshot_json")
    ]
    if not rows:
        print("No journaled merges available for exact reversal.")
        print("(Merges performed before the ENTITY_MERGE saga shipped are legacy: --legacy-report)")
        return 0
    print(f"{len(rows)} journaled merge(s) reversible EXACTLY:\n")
    for r in rows:
        req = json.loads(r["request_json"])
        print(f"  op={r['op_id']}  {req['survivor_uuid']}  <-  {req['absorbed_uuid']}")
        print(f"      committed_at={r.get('committed_at')}  similarity={req.get('similarity')}")
    print("\nReverse one with:  python scripts/unmerge.py --op <OP_ID> [--execute]")
    return 0


def _inventory(neo4j, journal: GraphOperationsJournal) -> int:
    """Read-only recoverability inventory. Writes nothing, invents nothing."""
    from menhir.services.merge_recoverability import inventory

    report = inventory(neo4j=neo4j, journal=journal)
    print(report.summary())
    return 0


def _legacy_unmerge(coordinator, absorbed: list[str], execute: bool) -> int:
    """Degraded recovery of pre-journal merges, gated on an explicit manifest.

    The manifest IS the list of --absorbed uuids: nothing outside it can be touched. Without
    --execute this reports what would be restored and, just as importantly, what cannot be.
    """
    from menhir.services.legacy_unmerge_coordinator import LegacyUnmergeCoordinator

    assert isinstance(coordinator, LegacyUnmergeCoordinator)
    failures = 0
    for uuid in absorbed:
        res = coordinator.unmerge_legacy(
            uuid, manifest=absorbed, acknowledge_degraded=execute
        )
        print(f"\n=== {uuid}")
        if res.get("degradations"):
            print("  DEGRADED RECOVERY -- this snapshot structurally CANNOT restore:")
            for d in res["degradations"]:
                print(f"    - {d}")
        if res.get("restored"):
            print(f"  restored (op={res['op_id']}), exact={res['exact']}")
            print(f"    out_restored={res.get('out_restored')} in_restored={res.get('in_restored')}"
                  f" bridges_removed={res.get('bridges_removed')}")
            print("    survivor NOT restored: its pre-merge state was never captured.")
        elif res.get("would_restore"):
            w = res["would_restore"]
            print(f"  WOULD restore {w['absorbed_uuid']} into survivor {w['survivor_uuid']}:")
            print(f"    labels:     {w['labels']}  (labels were never recorded)")
            print(f"    properties: {w['properties']}")
            print(f"    relationships: {w['out_relationships']} out, {w['in_relationships']} in")
            print("    survivor restored: NO")
            print("\n  Re-run with --execute to apply this DEGRADED recovery.")
        else:
            failures += 1
            print(f"  REFUSED: {res.get('reason')}")
            if res.get("diagnostics"):
                print(f"    {json.dumps(res['diagnostics'], default=str)}")
    return 1 if failures else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--op", help="the ENTITY_MERGE operation id to reverse")
    ap.add_argument("--execute", action="store_true",
                    help="perform the unmerge (default is a dry run)")
    ap.add_argument("--list", action="store_true", help="list journaled merges reversible exactly")
    ap.add_argument("--inventory", action="store_true",
                    help="read-only recoverability inventory of ALL merge history")
    ap.add_argument("--legacy-absorbed", nargs="+", metavar="UUID",
                    help="DEGRADED recovery of pre-journal merges. These uuids ARE the manifest: "
                         "nothing outside this list can be touched. Never an exact restoration.")
    args = ap.parse_args()

    settings = MemorySettings.from_env()
    coordinator, legacy, journal, neo4j = _build(settings)

    if args.inventory:
        return _inventory(neo4j, journal)
    if args.legacy_absorbed:
        return _legacy_unmerge(legacy, args.legacy_absorbed, args.execute)
    if args.list or not args.op:
        return _list_reversible(journal)

    result = coordinator.unmerge(args.op, dry_run=not args.execute)

    if result.get("restored"):
        print(f"UNMERGED (op={result.get('op_id')}):")
        for k in ("out_restored", "in_restored", "bridges_removed", "mentions_removed"):
            if k in result:
                print(f"  {k}: {result[k]}")
        return 0

    reason = result.get("reason")
    if reason == "DRY_RUN":
        w = result["would_restore"]
        print(f"DRY RUN -- would restore absorbed node {w['absorbed_uuid']}:")
        print(f"  labels:              {w['labels']}")
        print(f"  properties:          {w['properties']}")
        print(f"  out relationships:   {w['out_relationships']}")
        print(f"  in relationships:    {w['in_relationships']}")
        print(f"  survivor restored to: {w['survivor_properties_restored']}")
        print(f"  MENTIONS to remove from survivor: {w['rebound_episodes_to_remove']}")
        print("\nRe-run with --execute to apply.")
        return 0

    # Every refusal is explicit and diagnostic -- never a silent partial restore.
    print(f"REFUSED: {reason}", file=sys.stderr)
    print(json.dumps(result.get("diagnostics", {}), indent=2, default=str), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
