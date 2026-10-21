"""Plan/apply/verify command implementations for the recall-hygiene migration.

Extracted verbatim from ``migrate_flagged_bootstrap_scope.py``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from menhir.infrastructure.neo4j import Neo4jRepository

from migrate_flagged_bootstrap_scope_candidates import (
    ALL_CANDIDATE_KINDS,
    BOOTSTRAP_CANDIDATE,
    MANIFEST_KIND,
    MANIFEST_VERSION,
    _candidates,
    fingerprint,
    selected_kinds,
)
from migrate_flagged_bootstrap_scope_manifest import (
    _manifest_candidate,
    _read_jsonl,
    _write_jsonl,
    validate_manifest,
)


def cmd_plan(repo: Neo4jRepository, uri: str, args: argparse.Namespace) -> int:
    kinds = selected_kinds(args)
    candidates = _candidates(repo, limit=args.limit, kinds=kinds)
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
        #: Which kinds this manifest is authoritative for. A narrowed manifest covers
        #: only these, so apply/verify must not treat it as covering the whole graph.
        "candidate_kinds": sorted(kinds),
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
    problems = validate_manifest(
        header,
        candidates,
        uri=uri,
        max_rows=args.max_rows,
        kinds=selected_kinds(args) if getattr(args, "candidate_kind", None) else None,
    )
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
        requested = selected_kinds(args) if getattr(args, "candidate_kind", None) else None
        problems = validate_manifest(
            header, candidates, uri=uri, max_rows=args.limit, kinds=requested
        )
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
        # Sweep only what this manifest claims to cover. A narrowed manifest must not
        # fail because rows of an unrelated kind are still unreviewed -- and a cleaned
        # scoped_entity_cleanup row legitimately reclassifies as bootstrap_scope once
        # its pin is null, so it leaves this kind's population by design.
        covered = frozenset(
            str(kind) for kind in (header.get("candidate_kinds") or ALL_CANDIDATE_KINDS)
        )
        remaining = _candidates(repo, limit=args.limit, kinds=covered)
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

    remaining = _candidates(repo, limit=args.limit, kinds=selected_kinds(args))
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
