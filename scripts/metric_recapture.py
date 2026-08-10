"""One-time recapture of instrumentation :Entity view nodes as :Metric nodes.

The producers now write instrumentation to :Metric (Plan 2 step 4), but ~288 legacy counters are
still :Entity nodes polluting the semantic recall surface. They are NOT migrated: they are FOLDS
over telemetry rows that are still retained, so deleting them and letting the next nightly sweep
recompute them reproduces them EXACTLY, as :Metric. Recapture IS the recovery path -- which is why
there is no reversible migration saga here.

    plan   -> READ-ONLY. Classifies candidates, runs the whole-batch preflight, writes a manifest
              with the exact UUID list and a per-node fingerprint. Touches nothing.
    apply  -> DESTRUCTIVE. Deletes strictly the UUIDs listed in a manifest you have reviewed, and
              only after re-verifying every node still matches its recorded fingerprint. It never
              re-evaluates the classifier predicate at apply time and never deletes by degree.
    verify -> READ-ONLY. Residual scan: no candidates left, and the protected nodes still exist.

SCOPE (deliberately narrower than the classifier could be): only `source IN ('failure-telemetry',
'revision-telemetry')` -- the provably re-foldable classes. The ~25 perception/correction run-tally
:Entity nodes are LEFT IN PLACE: they are per-run observations with no backing telemetry table, so a
delete would lose them until the job happens to run again. New tallies write as :Metric and the old
:Entity ones supersede themselves out of currency naturally, at zero risk.

MANDATORY GATE before `apply` against production:
  1. take a backup FIRST:  python scripts/export_graph_backup.py
  2. python scripts/metric_recapture.py plan --out manifest.json
  3. MANUALLY read the manifest -- confirm every UUID is instrumentation, and that the real user
     facts and the verifier-sync registers are NOT in it (the manifest lists them explicitly under
     "protected" so you can check).
  4. python scripts/metric_recapture.py apply --manifest manifest.json --backup <path> --yes
  5. python scripts/metric_recapture.py verify

The delete is uuid-scoped and manifest-driven. It is NEVER a live predicate and NEVER a degree-zero
sweep -- that unscoped `NOT EXISTS { MATCH (n)-[]-() } DETACH DELETE` is exactly what destroyed 24
production nodes and got removed in Plan 1.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from menhir.config import MemorySettings
from menhir.infrastructure.neo4j import Neo4jRepository

#: The only sources eligible for delete-and-refold. Both are folds over retained telemetry tables
#: (`failure_events`, `memory_revisions`), so the next sweep recomputes them exactly.
REFOLDABLE_SOURCES = ("failure-telemetry", "revision-telemetry")

#: Relationship types a legitimate instrumentation counter may carry. Anything else means the node
#: is entangled in topology this script does not understand -> the whole batch is rejected.
ALLOWED_REL_TYPES = {"MENTIONS", "SUPERSEDES"}


def _fingerprint(row: dict[str, Any]) -> str:
    """Identity + everything a delete decision depends on. Re-checked at apply time: if any of this
    changed since the manifest was written, the snapshot is stale and the batch is rejected."""
    basis = "|".join(str(row.get(k)) for k in (
        "uuid", "source", "view_kind", "view_key", "view_value", "view_current", "group_id",
    )) + "|" + ",".join(sorted(str(x) for x in (row.get("labels") or [])))
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]


def _candidates(repo: Neo4jRepository) -> list[dict[str, Any]]:
    """READ-ONLY. Every :Entity counter view written by a re-foldable telemetry source, with the
    topology facts the preflight needs. `NOT n:Metric` keeps an already-recaptured node out."""
    rows = repo.execute(
        """
        MATCH (n:Entity)
        WHERE NOT n:Metric
          AND n.view_kind = 'counter'
          AND n.source IN $sources
        RETURN n.uuid AS uuid, n.source AS source, n.view_kind AS view_kind,
               n.view_key AS view_key, n.view_value AS view_value,
               coalesce(n.view_current, true) AS view_current, n.group_id AS group_id,
               n.name AS name, labels(n) AS labels,
               [(n)-[r]-() | type(r)] AS rel_types,
               EXISTS { MATCH (n)-[:VERIFIED_BY]-() } AS verifier_linked
        ORDER BY n.uuid
        """,
        {"sources": list(REFOLDABLE_SOURCES)},
    )
    return [dict(r) for r in rows]


def _protected(repo: Neo4jRepository) -> list[dict[str, Any]]:
    """READ-ONLY. The nodes that MUST survive: every current :Entity counter that is NOT a
    re-foldable-telemetry node -- the real user facts, the verifier-sync registers, and the
    perception/correction run-tallies this script deliberately leaves alone. The manifest carries
    this list so the operator reviewing it can see, in one place, what is being kept."""
    rows = repo.execute(
        """
        MATCH (n:Entity)
        WHERE NOT n:Metric
          AND n.view_kind = 'counter'
          AND coalesce(n.view_current, true)
          AND NOT coalesce(n.source, '') IN $sources
        RETURN n.uuid AS uuid, n.source AS source, n.view_key AS view_key,
               n.view_value AS view_value, n.name AS name
        ORDER BY n.view_key
        """,
        {"sources": list(REFOLDABLE_SOURCES)},
    )
    return [dict(r) for r in rows]


def _preflight(repo: Neo4jRepository, cands: list[dict[str, Any]]) -> list[str]:
    """Whole-batch rejection checks (plan F1). ANY violation fails the entire batch -- a partial
    delete of a batch we do not fully understand is worse than no delete."""
    problems: list[str] = []
    for c in cands:
        u = c["uuid"]
        if c.get("verifier_linked"):
            problems.append(f"{u}: participates in verifier topology (VERIFIED_BY)")
        if "Metric" in (c.get("labels") or []):
            problems.append(f"{u}: carries BOTH :Entity and :Metric")
        bad = sorted(set(c.get("rel_types") or []) - ALLOWED_REL_TYPES)
        if bad:
            problems.append(f"{u}: unapproved relationship type(s) {bad}")

    uuids = [c["uuid"] for c in cands]
    if uuids:
        collisions = repo.execute(
            "MATCH (m:Metric) WHERE m.uuid IN $uuids RETURN m.uuid AS uuid", {"uuids": uuids})
        for r in collisions:
            problems.append(f"{dict(r)['uuid']}: uuid collides with an existing :Metric")
    return problems


# ------------------------------------------------------------------------------------------ plan


def cmd_plan(repo: Neo4jRepository, uri: str, args: argparse.Namespace) -> int:
    cands = _candidates(repo)
    protected = _protected(repo)
    problems = _preflight(repo, cands)

    by_source: dict[str, int] = {}
    for c in cands:
        by_source[str(c.get("source"))] = by_source.get(str(c.get("source")), 0) + 1

    manifest = {
        "kind": "menhir-metric-recapture-manifest",
        "version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_uri": uri,
        "scope": {"sources": list(REFOLDABLE_SOURCES),
                  "note": "delete-and-refold only; perception/correction run-tallies are NOT here"},
        "counts": {"candidates": len(cands), "by_source": by_source, "protected": len(protected)},
        "preflight": {"ok": not problems, "problems": problems},
        "candidates": [
            {"uuid": c["uuid"], "source": c["source"], "view_key": c["view_key"],
             "view_value": c["view_value"], "view_current": c["view_current"],
             "name": c["name"], "fingerprint": _fingerprint(c)}
            for c in cands
        ],
        "protected": protected,
    }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"READ-ONLY plan against {uri}")
    print(f"  candidates: {len(cands)}  {by_source}")
    print(f"  protected (must survive): {len(protected)}")
    for p in protected:
        print(f"    KEEP  {p['view_key']}  = {p['view_value']}  (source={p['source']})")
    if problems:
        print(f"\n  PREFLIGHT FAILED -- {len(problems)} problem(s); apply will refuse this manifest:")
        for p in problems[:20]:
            print(f"    {p}")
    print(f"\nManifest -> {out}")
    print("REVIEW IT BY HAND before apply. Confirm every candidate is instrumentation and that "
          "every real fact / verifier register appears under \"protected\", not \"candidates\".")
    return 0 if not problems else 2


# ----------------------------------------------------------------------------------------- apply


def cmd_apply(repo: Neo4jRepository, uri: str, args: argparse.Namespace) -> int:
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    if manifest.get("kind") != "menhir-metric-recapture-manifest":
        print("ERROR: not a recapture manifest", file=sys.stderr)
        return 2
    if not manifest.get("preflight", {}).get("ok"):
        print("ERROR: manifest failed preflight at plan time; refusing to apply", file=sys.stderr)
        return 2
    if manifest.get("source_uri") != uri:
        print(f"ERROR: manifest was planned against {manifest.get('source_uri')}, not {uri}",
              file=sys.stderr)
        return 2

    backup = Path(args.backup)
    if not backup.is_file() or backup.stat().st_size == 0:
        print(f"ERROR: backup {backup} missing or empty. Take one FIRST:\n"
              "  python scripts/export_graph_backup.py", file=sys.stderr)
        return 2

    listed = manifest.get("candidates") or []
    if not listed:
        print("Nothing to delete.")
        return 0
    uuids = [c["uuid"] for c in listed]
    expected = {c["uuid"]: c["fingerprint"] for c in listed}

    # Re-read the EXACT reviewed UUIDs (never the classifier predicate) and prove nothing drifted
    # between review and apply. A node that changed, vanished, or grew new topology fails the batch.
    rows = repo.execute(
        """
        MATCH (n:Entity) WHERE n.uuid IN $uuids
        RETURN n.uuid AS uuid, n.source AS source, n.view_kind AS view_kind,
               n.view_key AS view_key, n.view_value AS view_value,
               coalesce(n.view_current, true) AS view_current, n.group_id AS group_id,
               labels(n) AS labels, [(n)-[r]-() | type(r)] AS rel_types
        """,
        {"uuids": uuids},
    )
    live = {str(dict(r)["uuid"]): dict(r) for r in rows}

    drift: list[str] = []
    for u, fp in expected.items():
        cur = live.get(u)
        if cur is None:
            drift.append(f"{u}: no longer present")
        elif _fingerprint(cur) != fp:
            drift.append(f"{u}: changed since the manifest was reviewed")
        else:
            bad = sorted(set(cur.get("rel_types") or []) - ALLOWED_REL_TYPES)
            if bad:
                drift.append(f"{u}: grew unapproved relationship type(s) {bad}")
    if drift:
        print(f"ERROR: {len(drift)} node(s) drifted since plan; re-run plan and re-review.",
              file=sys.stderr)
        for d in drift[:20]:
            print(f"  {d}", file=sys.stderr)
        return 2

    print(f"About to DELETE {len(uuids)} reviewed instrumentation node(s) from {uri}")
    print(f"  backup: {backup}")
    if not args.yes:
        print("\nDry run (no --yes). Nothing deleted.")
        return 0

    deleted = repo.execute(
        "MATCH (n:Entity) WHERE n.uuid IN $uuids DETACH DELETE n RETURN count(*) AS n",
        {"uuids": uuids},
    )
    n = int(dict(deleted[0])["n"]) if deleted else 0
    print(f"Deleted {n} node(s). The next nightly sweep re-folds them as :Metric.")
    return 0


# ---------------------------------------------------------------------------------------- verify


def cmd_verify(repo: Neo4jRepository, uri: str, args: argparse.Namespace) -> int:
    residual = _candidates(repo)
    protected = _protected(repo)
    metrics = repo.execute("MATCH (m:Metric) RETURN count(m) AS n")
    n_metrics = int(dict(metrics[0])["n"]) if metrics else 0

    print(f"Residual scan against {uri}")
    print(f"  re-foldable :Entity instrumentation left: {len(residual)}  (want 0)")
    print(f"  surviving non-telemetry :Entity counters: {len(protected)}")
    for p in protected:
        print(f"    {p['view_key']} = {p['view_value']}  (source={p['source']})")
    print(f"  :Metric nodes: {n_metrics}")
    return 0 if not residual else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--uri", help="Override Neo4j URI (default: from .env / MemorySettings)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("plan", help="READ-ONLY: classify + preflight, write a reviewable manifest")
    p.add_argument("--out", default="metric-recapture-manifest.json")
    p.set_defaults(fn=cmd_plan)

    a = sub.add_parser("apply", help="DESTRUCTIVE: delete exactly the reviewed manifest UUIDs")
    a.add_argument("--manifest", required=True)
    a.add_argument("--backup", required=True, help="Path to the backup taken BEFORE this delete")
    a.add_argument("--yes", action="store_true", help="Actually delete (otherwise dry run)")
    a.set_defaults(fn=cmd_apply)

    v = sub.add_parser("verify", help="READ-ONLY: residual scan")
    v.set_defaults(fn=cmd_verify)

    args = ap.parse_args()
    s = MemorySettings.from_env()
    uri = args.uri or s.neo4j_uri
    repo = Neo4jRepository(uri=uri, database=s.neo4j_database, user=s.neo4j_user,
                           password=s.neo4j_password)
    try:
        return int(args.fn(repo, uri, args))
    finally:
        close = getattr(repo, "close", None)
        if callable(close):
            close()


if __name__ == "__main__":
    raise SystemExit(main())
