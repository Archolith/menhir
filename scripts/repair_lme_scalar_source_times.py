#!/usr/bin/env python3
"""Repair scalar world time from a frozen LongMemEval fixture without re-extraction.

Historical LME imports written before TurnEvidence accepted ``occurred_at`` stamped both
``:TurnEvidence`` and ``:TypedAssertion.valid_at`` with server receive time. The frozen fixture
still contains the authoritative session date, and the importer namespace-qualifies each
``TurnEvidence.session_id`` deterministically, so the original world time is exactly recoverable.

This migration is dry-run by default. Apply requires:

* an explicit fixture SHA-256;
* an explicit namespace-prefix confirmation;
* a verified portable graph snapshot made before the write; and
* a new audit-output path.

It changes only ``TurnEvidence.occurred_at`` and ``TypedAssertion.valid_at`` before rebuilding
disposable ScalarStateView projections from the repaired assertion log. It never calls an LLM,
re-ingests an episode, or changes assertion identity.

Example:

    PYTHONPATH=src python scripts/repair_lme_scalar_source_times.py \
      --uri bolt://127.0.0.1:7794 \
      --fixture ../archolith-bench/fixtures/longmemeval/knowledge_update_subset.json \
      --prefix lme-

    PYTHONPATH=src python scripts/repair_lme_scalar_source_times.py \
      --uri bolt://127.0.0.1:7794 \
      --fixture ../archolith-bench/fixtures/longmemeval/knowledge_update_subset.json \
      --prefix lme- --confirm-prefix lme- \
      --expected-fixture-sha256 <sha256> \
      --backup-proof /path/to/graph-snapshot.json \
      --audit-out /path/to/scalar-source-time-repair.json \
      --apply
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from menhir.infrastructure.neo4j import Neo4jRepository

# The repair_lme_scalar_source_times_* sibling modules live beside this script; make
# them importable in every load mode (direct script run, spec_from_file_location, -m).
_SCRIPT_DIR = str(Path(__file__).resolve().parent)
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

# Facade re-exports: every symbol moved to a repair_lme_scalar_source_times_* sibling
# stays importable from this module path.
from repair_lme_scalar_source_times_model import (  # noqa: E402
    DATE_FORMAT,
    REPAIR_SOURCE,
    QueryExecutor,
    RepairPlan,
    RepairRefusal,
    SourceTimeIndex,
    _fixture_hash,
)
from repair_lme_scalar_source_times_planning import (  # noqa: E402
    _as_datetime,
    _iso,
    _parse_fixture_date,
    _same_instant,
    build_repair_plan,
    load_source_time_index,
)
from repair_lme_scalar_source_times_apply import (  # noqa: E402
    _write_audit,
    apply_repair,
    verify_backup_proof,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Repair LME scalar world time from a frozen fixture; dry-run by default."
    )
    parser.add_argument("--uri", default="bolt://127.0.0.1:7694")
    parser.add_argument("--database", default="neo4j")
    parser.add_argument("--user", default="neo4j")
    parser.add_argument("--password", default=os.environ.get("MENHIR_BENCH_NEO4J_PASSWORD", ""))
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-prefix")
    parser.add_argument("--expected-fixture-sha256")
    parser.add_argument("--backup-proof", type=Path)
    parser.add_argument("--audit-out", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        source_times = load_source_time_index(args.fixture, args.prefix)
        repository = Neo4jRepository(
            uri=args.uri,
            database=args.database,
            user=args.user,
            password=args.password,
        )
        try:
            if not repository.ping():
                raise RepairRefusal(f"Neo4j is not reachable at {args.uri}")
            plan = build_repair_plan(repository, source_times, args.prefix)
            print(json.dumps(
                {
                    "mode": "apply" if args.apply else "dry-run",
                    "fixture_sha256": source_times.fixture_sha256,
                    "fixture_namespaces": len(source_times.namespaces),
                    **plan.summary(),
                },
                indent=2,
                sort_keys=True,
            ))
            if not args.apply:
                print("DRY RUN -- no graph writes.")
                return 0

            if args.confirm_prefix != args.prefix:
                raise RepairRefusal("--confirm-prefix must exactly match --prefix")
            expected_sha = str(args.expected_fixture_sha256 or "").lower()
            if expected_sha != source_times.fixture_sha256:
                raise RepairRefusal(
                    "--expected-fixture-sha256 does not match the fixture bytes"
                )
            if args.backup_proof is None:
                raise RepairRefusal("--backup-proof is required with --apply")
            if args.audit_out is None:
                raise RepairRefusal("--audit-out is required with --apply")
            backup = verify_backup_proof(args.backup_proof)
            audit = apply_repair(
                repository,
                plan,
                audit_path=args.audit_out,
                fixture_path=args.fixture,
                source_times=source_times,
                namespace_prefix=args.prefix,
                backup=backup,
            )
            print(json.dumps(
                {
                    "status": audit["status"],
                    "repair_id": audit["repair_id"],
                    "evidence_updated": audit["applied"]["evidence_updated"],
                    "assertions_updated": audit["applied"]["assertions_updated"],
                    "views_rebuilt": len(audit["rebuilds"]),
                },
                indent=2,
                sort_keys=True,
            ))
            return 0
        finally:
            repository.close()
    except RepairRefusal as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
