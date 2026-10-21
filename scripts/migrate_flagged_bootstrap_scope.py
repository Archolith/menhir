"""Plan and apply the one-time recall-hygiene classification cutover.

The migration has two responsibilities in one reviewed manifest:

* assign ``structure_role`` to deterministic legacy project-scan rows and clear
  invalid retention/bootstrap flags from every structural candidate;
* classify legacy flagged semantic memories as ``general``,
  ``workspace:<key>``, or explicit retention-only ``none`` pins.

``namespace`` and ``group_id`` are fingerprinted invariants and are never
written. The default ``plan`` and ``verify`` operations are read-only. ``apply``
requires a non-empty logical backup, a fully reviewed manifest, and ``--yes``.

Production sequence::

    python scripts/export_graph_backup.py
    python scripts/migrate_flagged_bootstrap_scope.py plan --out recall-hygiene.jsonl
    # Review every UNREVIEWED target in the manifest.
    python scripts/migrate_flagged_bootstrap_scope.py apply \
        --manifest recall-hygiene.jsonl --backup <backup.jsonl.gz>
    python scripts/migrate_flagged_bootstrap_scope.py apply \
        --manifest recall-hygiene.jsonl --backup <backup.jsonl.gz> --yes
    python scripts/migrate_flagged_bootstrap_scope.py verify --manifest recall-hygiene.jsonl
"""

from __future__ import annotations

# Retained at facade level so the module's import-time attribute surface is
# unchanged (tests reach MODULE.hashlib / MODULE.infer_legacy_structure_role, and
# CLI entry tooling may reach the rest).
import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from menhir.config import MemorySettings
from menhir.domain.bootstrap_scope import normalize_bootstrap_scope
from menhir.domain.structural_memory import (
    STRUCTURE_ROLES,
    infer_legacy_structure_role,
    legacy_structural_memory_cypher,
    non_structural_memory_cypher,
)
from menhir.infrastructure.neo4j import Neo4jRepository

# The migrate_flagged_bootstrap_scope_* sibling modules live beside this script; make
# them importable in every load mode (direct script run, spec_from_file_location, -m).
_SCRIPT_DIR = str(Path(__file__).resolve().parent)
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

# Facade re-exports: every symbol moved to a migrate_flagged_bootstrap_scope_* sibling
# stays importable from this module path.
from migrate_flagged_bootstrap_scope_candidates import (  # noqa: E402
    ALL_CANDIDATE_KINDS,
    BOOTSTRAP_CANDIDATE,
    DEFAULT_LIMIT,
    LEGACY_STRUCTURE_CANDIDATE,
    MANIFEST_KIND,
    MANIFEST_VERSION,
    SCOPED_ENTITY_CLEANUP_CANDIDATE,
    SEMANTIC_CANDIDATE_KINDS,
    STRUCTURAL_CANDIDATE_KINDS,
    STRUCTURAL_CLEANUP_CANDIDATE,
    UNREVIEWED,
    _candidates,
    _classify_candidate,
    fingerprint,
    selected_kinds,
)
from migrate_flagged_bootstrap_scope_manifest import (  # noqa: E402
    _manifest_candidate,
    _normalize_manifest_candidate,
    _read_jsonl,
    _write_jsonl,
    validate_manifest,
)
from migrate_flagged_bootstrap_scope_commands import (  # noqa: E402
    _evaluate_manifest,
    _expected_row,
    _load_live_rows,
    _matches_target,
    _print_errors,
    _target_state,
    cmd_apply,
    cmd_plan,
    cmd_verify,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uri", help="Override Neo4j URI")
    sub = parser.add_subparsers(dest="command")
    plan = sub.add_parser("plan", help="READ-ONLY: export a combined review manifest")
    plan.add_argument("--out", default="recall-hygiene.jsonl")
    plan.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    plan.add_argument(
        "--candidate-kind",
        action="append",
        choices=ALL_CANDIDATE_KINDS,
        help="Restrict to one candidate kind (repeatable). Default: every kind.",
    )
    plan.set_defaults(fn=cmd_plan)
    apply = sub.add_parser("apply", help="Dry-run or apply a reviewed manifest")
    apply.add_argument("--manifest", required=True)
    apply.add_argument("--backup", required=True)
    apply.add_argument("--max-rows", type=int, default=DEFAULT_LIMIT)
    apply.add_argument("--batch-size", type=int, default=100)
    apply.add_argument("--yes", action="store_true")
    apply.add_argument(
        "--candidate-kind",
        action="append",
        choices=ALL_CANDIDATE_KINDS,
        help="Restrict to one candidate kind (repeatable). Default: every kind.",
    )
    apply.set_defaults(fn=cmd_apply)
    verify = sub.add_parser("verify", help="READ-ONLY: verify migration candidates/idempotence")
    verify.add_argument("--manifest")
    verify.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    verify.add_argument(
        "--candidate-kind",
        action="append",
        choices=ALL_CANDIDATE_KINDS,
        help="Restrict to one candidate kind (repeatable). Default: every kind.",
    )
    verify.set_defaults(fn=cmd_verify)

    args = parser.parse_args()
    if args.command is None:
        args = parser.parse_args(["plan"])
    settings = MemorySettings.from_env()
    uri = args.uri or settings.neo4j_uri
    repo = Neo4jRepository(
        uri=uri,
        database=settings.neo4j_database,
        user=settings.neo4j_user,
        password=settings.neo4j_password,
    )
    try:
        return int(args.fn(repo, uri, args))
    finally:
        close = getattr(repo, "close", None)
        if callable(close):
            close()


if __name__ == "__main__":
    raise SystemExit(main())
