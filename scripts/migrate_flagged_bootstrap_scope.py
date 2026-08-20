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

MANIFEST_KIND = "menhir-recall-hygiene-jsonl"
MANIFEST_VERSION = 2
DEFAULT_LIMIT = 1000
UNREVIEWED = "UNREVIEWED"
BOOTSTRAP_CANDIDATE = "bootstrap_scope"
LEGACY_STRUCTURE_CANDIDATE = "legacy_structure"
STRUCTURAL_CLEANUP_CANDIDATE = "structural_cleanup"
#: Flagged semantic entities that already carry a bootstrap_scope. Until the fix in
#: services/enrichment_steps.py, propagate_user_flag forwarded a flagged episode's
#: bootstrap_scope onto every entity extracted from it, so shared hubs landed in the
#: startup read (which requires user_flagged AND an allowed scope). The node alone
#: cannot prove whether its scope was inherited or set deliberately via
#: flag_memory(uuid, bootstrap_scope=...), so every row is reviewed, never cleared
#: automatically. Kept separate from BOOTSTRAP_CANDIDATE so this cleanup does not drag
#: in the large retention-only population that has scope IS NULL.
SCOPED_ENTITY_CLEANUP_CANDIDATE = "scoped_entity_cleanup"
STRUCTURAL_CANDIDATE_KINDS = frozenset(
    {LEGACY_STRUCTURE_CANDIDATE, STRUCTURAL_CLEANUP_CANDIDATE}
)
#: Semantic kinds: retention is preserved, only the startup pin is reviewed.
SEMANTIC_CANDIDATE_KINDS = frozenset(
    {BOOTSTRAP_CANDIDATE, SCOPED_ENTITY_CLEANUP_CANDIDATE}
)
ALL_CANDIDATE_KINDS = (
    LEGACY_STRUCTURE_CANDIDATE,
    STRUCTURAL_CLEANUP_CANDIDATE,
    BOOTSTRAP_CANDIDATE,
    SCOPED_ENTITY_CLEANUP_CANDIDATE,
)


def fingerprint(row: dict[str, Any]) -> str:
    """Fingerprint every live value that the migration must preserve or classify."""

    basis = json.dumps(
        {
            "uuid": row.get("uuid"),
            "name": row.get("name"),
            "content": row.get("content"),
            "namespace": row.get("namespace"),
            "user_flagged": bool(row.get("user_flagged")),
            "bootstrap_scope": row.get("bootstrap_scope"),
            "structure_role": row.get("structure_role"),
            "labels": sorted(row.get("labels") or []),
            "source": row.get("source"),
            "group_id": row.get("group_id"),
            "anchor_projects": sorted(row.get("anchor_projects") or []),
            "anchor_paths": sorted(row.get("anchor_paths") or []),
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def _classify_candidate(row: dict[str, Any]) -> dict[str, Any]:
    """Attach the deterministic candidate kind and proposed structure role."""

    inferred_role = infer_legacy_structure_role(row)
    if inferred_role is not None:
        kind = (
            LEGACY_STRUCTURE_CANDIDATE
            if row.get("structure_role") is None
            else STRUCTURAL_CLEANUP_CANDIDATE
        )
        return {
            **row,
            "candidate_kind": kind,
            "proposed_structure_role": inferred_role,
        }
    if bool(row.get("user_flagged")) and row.get("bootstrap_scope") is None:
        return {
            **row,
            "candidate_kind": BOOTSTRAP_CANDIDATE,
            "proposed_structure_role": None,
        }
    if (
        "Entity" in (row.get("labels") or [])
        and bool(row.get("user_flagged"))
        and row.get("bootstrap_scope") is not None
    ):
        return {
            **row,
            "candidate_kind": SCOPED_ENTITY_CLEANUP_CANDIDATE,
            "proposed_structure_role": None,
        }
    raise ValueError(f"{row.get('uuid')}: row does not match a migration candidate kind")


def _candidates(repo: Neo4jRepository, *, limit: int) -> list[dict[str, Any]]:
    legacy_structure = legacy_structural_memory_cypher("n")
    non_structure = non_structural_memory_cypher("n")
    rows = repo.execute(
        f"""
        MATCH (n)
        WHERE (n:Entity OR n:Episodic)
          AND (
            (n:Entity AND n.structure_role IS NULL AND ({legacy_structure}))
            OR (
              n:Entity AND n.structure_role IS NOT NULL
              AND (coalesce(n.user_flagged, false) OR n.bootstrap_scope IS NOT NULL)
            )
            OR (
              coalesce(n.user_flagged, false)
              AND n.bootstrap_scope IS NULL
              AND ({non_structure})
            )
            OR (
              n:Entity
              AND coalesce(n.user_flagged, false)
              AND n.bootstrap_scope IS NOT NULL
              AND ({non_structure})
            )
          )
        RETURN n.uuid AS uuid, labels(n) AS labels, n.name AS name, n.content AS content,
               n.source AS source, n.group_id AS group_id,
               n.namespace AS namespace, n.user_flagged AS user_flagged,
               n.bootstrap_scope AS bootstrap_scope,
               n.structure_role AS structure_role,
               [(n)-[:ANCHORED_TO]->(a:Entity) WHERE a.structure_role IS NOT NULL |
                    a.structure_project] AS anchor_projects,
               [(n)-[:ANCHORED_TO]->(a:Entity) WHERE a.structure_role IS NOT NULL |
                    a.structure_path] AS anchor_paths
        ORDER BY n.uuid
        LIMIT $limit
        """,
        {"limit": limit + 1},
    )
    materialized = [dict(row) for row in rows]
    if len(materialized) > limit:
        raise ValueError(
            f"candidate count exceeds bounded limit {limit}; narrow the dataset or raise --limit explicitly"
        )
    return [_classify_candidate(row) for row in materialized]


def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def _read_jsonl(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    rows = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not rows or rows[0].get("kind") != MANIFEST_KIND:
        raise ValueError("not a recall-hygiene manifest")
    return rows[0], rows[1:]


def _manifest_candidate(row: dict[str, Any]) -> dict[str, Any]:
    candidate = {
        "kind": "candidate",
        **row,
        "summary_excerpt": str(row.get("content") or "")[:240],
        "fingerprint": fingerprint(row),
    }
    if row["candidate_kind"] in STRUCTURAL_CANDIDATE_KINDS:
        candidate.update(
            {
                "target_structure_role": UNREVIEWED,
                "target_user_flagged": False,
                "target_bootstrap_scope": "none",
            }
        )
    else:
        candidate.update(
            {
                "target_structure_role": None,
                "target_user_flagged": True,
                "target_bootstrap_scope": UNREVIEWED,
            }
        )
    return candidate


def cmd_plan(repo: Neo4jRepository, uri: str, args: argparse.Namespace) -> int:
    candidates = _candidates(repo, limit=args.limit)
    manifest_candidates = [_manifest_candidate(row) for row in candidates]
    candidate_fingerprints = [str(row["fingerprint"]) for row in manifest_candidates]
    counts = {
        kind: sum(row["candidate_kind"] == kind for row in manifest_candidates)
        for kind in ALL_CANDIDATE_KINDS
    }
    header = {
        "kind": MANIFEST_KIND,
        "version": MANIFEST_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_uri": uri,
        "candidate_count": len(manifest_candidates),
        "candidate_counts": counts,
        "limit": args.limit,
        "expected_graph_fingerprint": hashlib.sha256(
            "|".join(candidate_fingerprints).encode("utf-8")
        ).hexdigest(),
        "preserved_fields": ["namespace", "group_id", "relationships"],
        "instructions": (
            "For structure candidates, replace target_structure_role=UNREVIEWED with the reviewed "
            "role; structural flags/scopes will be cleared. For bootstrap and "
            "scoped_entity_cleanup candidates, replace target_bootstrap_scope=UNREVIEWED with "
            "general, workspace:<key>, or none; target_user_flagged stays true either way, so "
            "'none' clears the startup pin WITHOUT removing lifecycle retention. For "
            "scoped_entity_cleanup use none when the scope was inherited from a flagged episode, "
            "or restate the existing scope to keep a deliberate pin. "
            "namespace and group_id are fingerprinted invariants and are never written."
        ),
    }
    out = Path(args.out)
    _write_jsonl(out, [header, *manifest_candidates])
    print(
        f"READ-ONLY plan against {uri}: {len(manifest_candidates)} candidate(s) "
        f"{json.dumps(counts, sort_keys=True)}"
    )
    print(f"JSONL manifest -> {out}")
    print("No graph writes performed. Replace every UNREVIEWED target before apply.")
    return 0


def _load_live_rows(repo: Neo4jRepository, uuids: list[str]) -> dict[str, dict[str, Any]]:
    rows = repo.execute(
        """
        MATCH (n) WHERE n.uuid IN $uuids
        RETURN n.uuid AS uuid, labels(n) AS labels, n.name AS name, n.content AS content,
               n.source AS source, n.group_id AS group_id,
               n.namespace AS namespace, n.user_flagged AS user_flagged,
               n.bootstrap_scope AS bootstrap_scope,
               n.structure_role AS structure_role,
               [(n)-[:ANCHORED_TO]->(a:Entity) WHERE a.structure_role IS NOT NULL |
                    a.structure_project] AS anchor_projects,
               [(n)-[:ANCHORED_TO]->(a:Entity) WHERE a.structure_role IS NOT NULL |
                    a.structure_path] AS anchor_paths
        """,
        {"uuids": uuids},
    )
    return {str(dict(row)["uuid"]): dict(row) for row in rows}


def _normalize_manifest_candidate(row: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    uuid = row.get("uuid")
    kind = row.get("candidate_kind")
    if kind in STRUCTURAL_CANDIDATE_KINDS:
        target_role = str(row.get("target_structure_role") or "").strip().casefold()
        if not target_role or target_role == UNREVIEWED.casefold():
            problems.append(f"{uuid}: target_structure_role must be explicitly reviewed")
        elif target_role not in STRUCTURE_ROLES:
            problems.append(
                f"{uuid}: target_structure_role must be one of {sorted(STRUCTURE_ROLES)}"
            )
        else:
            row["target_structure_role"] = target_role
        if row.get("target_user_flagged") is not False:
            problems.append(f"{uuid}: structural candidates must set target_user_flagged=false")
        scope = row.get("target_bootstrap_scope")
        if scope is None or str(scope).strip().casefold() != "none":
            problems.append(f"{uuid}: structural candidates must explicitly use scope none")
        else:
            row["target_bootstrap_scope"] = None
    elif kind in SEMANTIC_CANDIDATE_KINDS:
        if row.get("target_structure_role") is not None:
            problems.append(f"{uuid}: semantic bootstrap candidates cannot set structure_role")
        if row.get("target_user_flagged") is not True:
            problems.append(f"{uuid}: bootstrap candidates must preserve target_user_flagged=true")
        target = row.get("target_bootstrap_scope")
        if target is None or str(target).strip().upper() == UNREVIEWED:
            problems.append(
                f"{uuid}: target_bootstrap_scope must be explicitly reviewed; "
                "use general, workspace:<key>, or none"
            )
        else:
            try:
                row["target_bootstrap_scope"] = normalize_bootstrap_scope(str(target))
            except ValueError as exc:
                problems.append(f"{uuid}: {exc}")
    else:
        problems.append(f"{uuid}: unknown candidate_kind {kind!r}")
    return problems


def validate_manifest(
    header: dict[str, Any], candidates: list[dict[str, Any]], *, uri: str, max_rows: int
) -> list[str]:
    problems: list[str] = []
    if int(header.get("version", -1)) != MANIFEST_VERSION:
        problems.append(f"manifest version must be {MANIFEST_VERSION}")
    if header.get("source_uri") != uri:
        problems.append("manifest source_uri does not match the current database")
    if len(candidates) != int(header.get("candidate_count", -1)):
        problems.append("manifest candidate count does not match its header")
    if len(candidates) > max_rows:
        problems.append(f"manifest has {len(candidates)} rows, exceeding bound {max_rows}")
    expected_graph_fingerprint = hashlib.sha256(
        "|".join(str(row.get("fingerprint") or "") for row in candidates).encode("utf-8")
    ).hexdigest()
    if header.get("expected_graph_fingerprint") != expected_graph_fingerprint:
        problems.append("manifest expected_graph_fingerprint does not match its candidate rows")
    uuids = [str(row.get("uuid") or "") for row in candidates]
    if not all(uuids) or len(set(uuids)) != len(uuids):
        problems.append("candidate UUIDs must be non-empty and unique")
    for row in candidates:
        problems.extend(_normalize_manifest_candidate(row))
    return problems


def _target_state(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "user_flagged": bool(row["target_user_flagged"]),
        "bootstrap_scope": row.get("target_bootstrap_scope"),
        "structure_role": row.get("target_structure_role"),
    }


def _expected_row(row: dict[str, Any]) -> dict[str, Any]:
    expected = dict(row)
    expected.update(_target_state(row))
    return expected


def _matches_target(current: dict[str, Any], row: dict[str, Any]) -> bool:
    target = _target_state(row)
    return (
        bool(current.get("user_flagged")) == target["user_flagged"]
        and current.get("bootstrap_scope") == target["bootstrap_scope"]
        and current.get("structure_role") == target["structure_role"]
    )


def _evaluate_manifest(
    live: dict[str, dict[str, Any]], candidates: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[str], list[str], list[str]]:
    pending: list[dict[str, Any]] = []
    unchanged: list[str] = []
    missing: list[str] = []
    drift: list[str] = []
    for row in candidates:
        uuid = str(row["uuid"])
        current = live.get(uuid)
        if current is None:
            missing.append(uuid)
        elif _matches_target(current, row):
            if fingerprint(current) == fingerprint(_expected_row(row)):
                unchanged.append(uuid)
            else:
                drift.append(f"{uuid}: preserved fields changed since plan")
        elif fingerprint(current) != row.get("fingerprint"):
            drift.append(f"{uuid}: changed since plan")
        else:
            pending.append(row)
    return pending, unchanged, missing, drift


def _print_errors(problems: Iterable[str]) -> None:
    for problem in problems:
        print(f"ERROR: {problem}", file=sys.stderr)


def cmd_apply(repo: Neo4jRepository, uri: str, args: argparse.Namespace) -> int:
    header, candidates = _read_jsonl(Path(args.manifest))
    problems = validate_manifest(header, candidates, uri=uri, max_rows=args.max_rows)
    if args.batch_size < 1 or args.batch_size > args.max_rows:
        problems.append("--batch-size must be between 1 and --max-rows")
    backup = Path(args.backup)
    if not backup.is_file() or backup.stat().st_size == 0:
        problems.append("backup is missing or empty")
    if problems:
        _print_errors(problems)
        return 2

    live = _load_live_rows(repo, [str(row["uuid"]) for row in candidates])
    pending, unchanged, missing, drift = _evaluate_manifest(live, candidates)
    if missing:
        _print_errors(f"{uuid}: unknown target UUID" for uuid in missing)
    if drift:
        _print_errors(drift)
    if missing or drift:
        return 2

    pending_counts = {
        kind: sum(row["candidate_kind"] == kind for row in pending)
        for kind in ALL_CANDIDATE_KINDS
    }
    print(
        f"Validated {len(candidates)} reviewed row(s): pending={len(pending)}, "
        f"unchanged={len(unchanged)}, missing=0, pending_by_kind="
        f"{json.dumps(pending_counts, sort_keys=True)}"
    )
    if not args.yes:
        print("Dry run (no --yes). Nothing written.")
        return 0

    changed = 0
    for start in range(0, len(pending), args.batch_size):
        batch = [
            {"uuid": row["uuid"], **_target_state(row)}
            for row in pending[start : start + args.batch_size]
        ]
        rows = repo.execute(
            """
            UNWIND $rows AS row
            MATCH (n {uuid: row.uuid})
            SET n.structure_role = row.structure_role,
                n.user_flagged = row.user_flagged,
                n.bootstrap_scope = row.bootstrap_scope
            RETURN count(n) AS updated
            """,
            {"rows": batch},
        )
        changed += int(dict(rows[0]).get("updated", 0)) if rows else 0

    post_live = _load_live_rows(repo, [str(row["uuid"]) for row in candidates])
    post_pending, _post_unchanged, post_missing, post_drift = _evaluate_manifest(
        post_live, candidates
    )
    if changed != len(pending) or post_pending or post_missing or post_drift:
        _print_errors(
            [
                f"post-apply verification failed: expected_updates={len(pending)} actual={changed}",
                *post_missing,
                *post_drift,
                *(f"{row['uuid']}: target state not applied" for row in post_pending),
            ]
        )
        return 2
    print(
        f"Apply complete: changed={changed}, unchanged={len(unchanged)}, missing=0. "
        "namespace/group_id were not written."
    )
    return 0


def cmd_verify(repo: Neo4jRepository, uri: str, args: argparse.Namespace) -> int:
    if args.manifest:
        header, candidates = _read_jsonl(Path(args.manifest))
        problems = validate_manifest(header, candidates, uri=uri, max_rows=args.limit)
        if problems:
            _print_errors(problems)
            return 2
        live = _load_live_rows(repo, [str(row["uuid"]) for row in candidates])
        pending, unchanged, missing, drift = _evaluate_manifest(live, candidates)
        reviewed_retention_only = {
            str(row["uuid"])
            for row in candidates
            if row["candidate_kind"] == BOOTSTRAP_CANDIDATE
            and row.get("target_bootstrap_scope") is None
        }
        remaining = _candidates(repo, limit=args.limit)
        unexpected = [
            str(row["uuid"])
            for row in remaining
            if str(row["uuid"]) not in reviewed_retention_only
        ]
        print(
            f"READ-ONLY manifest verify against {uri}: reviewed={len(candidates)}, "
            f"verified={len(unchanged)}, pending={len(pending)}, "
            f"unexpected_candidates={len(unexpected)}, drift={len(drift)}, missing={len(missing)}"
        )
        if pending or unexpected or drift or missing:
            _print_errors(
                [
                    *(f"{row['uuid']}: pending target state" for row in pending),
                    *(f"{uuid}: unreviewed candidate remains" for uuid in unexpected),
                    *drift,
                    *(f"{uuid}: missing" for uuid in missing),
                ]
            )
            return 2
        print(
            "Idempotence verified: zero pending writes, zero unreviewed candidates, "
            "and preserved fields match the manifest."
        )
        return 0

    remaining = _candidates(repo, limit=args.limit)
    counts = {
        kind: sum(row["candidate_kind"] == kind for row in remaining)
        for kind in ALL_CANDIDATE_KINDS
    }
    print(
        f"READ-ONLY verify against {uri}: {len(remaining)} candidate(s) "
        f"{json.dumps(counts, sort_keys=True)}"
    )
    print("Pass --manifest for a reviewed idempotence verdict.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uri", help="Override Neo4j URI")
    sub = parser.add_subparsers(dest="command")
    plan = sub.add_parser("plan", help="READ-ONLY: export a combined review manifest")
    plan.add_argument("--out", default="recall-hygiene.jsonl")
    plan.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    plan.set_defaults(fn=cmd_plan)
    apply = sub.add_parser("apply", help="Dry-run or apply a reviewed manifest")
    apply.add_argument("--manifest", required=True)
    apply.add_argument("--backup", required=True)
    apply.add_argument("--max-rows", type=int, default=DEFAULT_LIMIT)
    apply.add_argument("--batch-size", type=int, default=100)
    apply.add_argument("--yes", action="store_true")
    apply.set_defaults(fn=cmd_apply)
    verify = sub.add_parser("verify", help="READ-ONLY: verify migration candidates/idempotence")
    verify.add_argument("--manifest")
    verify.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
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
