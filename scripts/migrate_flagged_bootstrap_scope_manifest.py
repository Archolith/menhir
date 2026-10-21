"""Manifest serialization, review normalization, and validation.

Extracted verbatim from ``migrate_flagged_bootstrap_scope.py``.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

from menhir.domain.bootstrap_scope import normalize_bootstrap_scope
from menhir.domain.structural_memory import STRUCTURE_ROLES

from migrate_flagged_bootstrap_scope_candidates import (
    ALL_CANDIDATE_KINDS,
    MANIFEST_KIND,
    MANIFEST_VERSION,
    SEMANTIC_CANDIDATE_KINDS,
    STRUCTURAL_CANDIDATE_KINDS,
    UNREVIEWED,
    fingerprint,
)


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
    header: dict[str, Any],
    candidates: list[dict[str, Any]],
    *,
    uri: str,
    max_rows: int,
    kinds: frozenset[str] | None = None,
) -> list[str]:
    problems: list[str] = []
    declared = header.get("candidate_kinds")
    if declared is not None:
        declared_set = frozenset(str(kind) for kind in declared)
        unknown = sorted(declared_set - frozenset(ALL_CANDIDATE_KINDS))
        if unknown:
            problems.append(f"manifest declares unknown candidate_kinds {unknown}")
        stray = sorted({str(row.get("candidate_kind")) for row in candidates} - declared_set)
        if stray:
            problems.append(
                f"manifest contains rows of kind {stray}, outside its declared coverage "
                f"{sorted(declared_set)}"
            )
        if kinds is not None and declared_set != kinds:
            problems.append(
                f"--candidate-kind {sorted(kinds)} does not match this manifest's coverage "
                f"{sorted(declared_set)}"
            )
    elif kinds is not None and kinds != frozenset(ALL_CANDIDATE_KINDS):
        # A pre-coverage manifest cannot prove which kinds it was planned for, so a
        # narrowed apply/verify against one would be guessing.
        problems.append(
            "manifest predates candidate_kinds coverage; re-plan before using --candidate-kind"
        )
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
