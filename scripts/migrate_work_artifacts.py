"""Compatibility wrapper around `menhir artifacts` — one corpus collector only.

This script used to be its own collector: a one-level scan of four directories
that created nodes keyed by locator and never reconciled an existing source.
That produced the failure the reconciliation work exists to fix -- backlog
records it could not see, and a duplicate identity for every document that had
moved since it last ran.

It no longer scans or writes anything itself. Audit and apply are delegated to
``menhir.services.artifact_reconciliation_service``, which is the only corpus
collector, so this entry point cannot drift from the reconciler again.

``--revalidate`` is retained and still lives here: shape validation is a
different question from source reconciliation (is this document well-formed for
its type, rather than where does it live), and it has no reconciliation
equivalent.

Prefer the CLI directly:

    menhir artifacts audit --repo <path> [--json]
    menhir artifacts reconcile --repo <path> --apply --plan-digest <digest>
"""

from __future__ import annotations

import argparse
import os
from collections import Counter
from pathlib import Path

from dotenv import load_dotenv

from menhir.config.settings_model import MemorySettings
from menhir.infrastructure.neo4j import Neo4jRepository
from menhir.infrastructure.work_artifact_repository import WorkArtifactRepository
from menhir.services.artifact_reconciliation_service import (
    ArtifactReconciliationService,
)

MENHIR_ROOT = Path(__file__).resolve().parents[1]
# Second artifact tree outside the repo, if the operator keeps one. Defaults to the repo
# itself so a plain clone scans only what it ships; set WORKSPACE_ROOT to point elsewhere.
WORKSPACE_ROOT = Path(os.getenv("WORKSPACE_ROOT") or MENHIR_ROOT)


def _connect() -> WorkArtifactRepository:
    load_dotenv()
    s = MemorySettings.from_env()
    return WorkArtifactRepository(Neo4jRepository(
        uri=s.neo4j_uri, user=s.neo4j_user, password=s.neo4j_password, database=s.neo4j_database
    ))


def _revalidate() -> int:
    """Re-read every ingested artifact and store a fresh shape verdict.

    A verdict describes the document as it is now, so it has to be recomputed
    rather than inherited from ingest time.
    """
    repo = _connect()
    rows = repo.neo4j.execute(
        "MATCH (a:WorkArtifact)-[:EMBODIED_IN]->(s:ArtifactSource) "
        "RETURN a.artifact_uuid AS uuid, a.artifact_type AS t, "
        "       s.locator_repository AS repo, s.locator_path AS path",
        {},
    )

    roots = {"menhir": MENHIR_ROOT, "IdeaProjects": WORKSPACE_ROOT}
    tally: Counter = Counter()
    violations: Counter = Counter()
    missing = 0

    for row in rows:
        root = roots.get(row["repo"])
        if root is None or not row["path"]:
            missing += 1
            continue
        path = root / row["path"]
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            # The file is gone; that is a real finding, not a crash.
            text = None
            missing += 1
        report = repo.record_shape(row["uuid"], row["t"], text)
        tally[report.status] += 1
        for finding in report.required_failures:
            violations[finding.code] += 1

    print(f"revalidated       : {len(rows)} artifacts")
    print(f"  status          : {dict(tally)}")
    print(f"  unreadable      : {missing}")
    print("  top violations  :")
    for code, count in violations.most_common(10):
        print(f"    {count:>4}  {code}")
    return 0


def _report(root: Path, repository: str, apply: bool, plan_digest: str) -> int:
    service = ArtifactReconciliationService(_connect())
    report = service.audit(root, repository=repository)
    counts = report.counts

    print(f"corpus            : {counts.get('entries', 0)} records in {repository}")
    print(f"  graph sources   : {counts.get('sources', 0)}")
    print(f"  by lane         : {counts.get('entries_by_lane', {})}")
    print(f"  by type         : {counts.get('entries_by_type', {})}")
    print(f"  proposed        : {counts.get('by_kind', {})}")
    print(f"  match basis     : {counts.get('by_basis', {})}")
    print(f"  conflicts       : {counts.get('by_conflict', {})}")
    print(f"  plan digest     : {report.plan_digest}")

    if not apply:
        print("\nDRY RUN -- nothing written.")
        print(
            "Apply with: menhir artifacts reconcile "
            f"--repo {root} --apply --plan-digest {report.plan_digest}"
        )
        return 0

    result = service.apply(root, expected_digest=plan_digest, repository=repository)
    if not result.ok:
        print(f"\nrefused: {result.refused_reason}; current digest {result.plan_digest}")
        return 2
    print(
        f"\napplied: {len(result.applied)} written, {len(result.skipped)} skipped, "
        f"{len(result.conflicted)} conflicted"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    ap.add_argument("--plan-digest", default="", help="digest of the approved audit ledger")
    ap.add_argument("--repo", default=str(MENHIR_ROOT), help="repository root to reconcile")
    ap.add_argument("--repository", default="", help="repository name recorded on sources")
    ap.add_argument("--revalidate", action="store_true",
                    help="re-run shape validation over already-ingested artifacts")
    args = ap.parse_args(argv)

    if args.revalidate:
        return _revalidate()

    root = Path(args.repo).resolve()
    if args.apply and not args.plan_digest:
        print("--apply requires --plan-digest from an approved audit ledger.")
        return 2
    return _report(root, args.repository or root.name, args.apply, args.plan_digest)


if __name__ == "__main__":
    raise SystemExit(main())
