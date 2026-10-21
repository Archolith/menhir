"""Candidate constants, fingerprinting, classification, and bounded scan.

Extracted verbatim from ``migrate_flagged_bootstrap_scope.py``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from typing import Any

from menhir.domain.structural_memory import (
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


def _candidates(
    repo: Neo4jRepository, *, limit: int, kinds: frozenset[str] | None = None
) -> list[dict[str, Any]]:
    """Return classified candidates, optionally narrowed to specific kinds.

    ``limit`` always bounds the SCAN, never the filtered result, so a narrowed run
    still fails closed when the underlying population exceeds the bound: you either
    see every row of the selected kind or you get an error, never a silent subset.
    Filtering happens after classification because ``_classify_candidate`` is the
    source of truth (it runs ``infer_legacy_structure_role``, which the Cypher
    predicate cannot reproduce) -- duplicating the kind rules in the query would let
    the two drift apart.
    """
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
    classified = [_classify_candidate(row) for row in materialized]
    if kinds is None:
        return classified
    return [row for row in classified if row["candidate_kind"] in kinds]


def selected_kinds(args: argparse.Namespace) -> frozenset[str]:
    """Resolve --candidate-kind into an explicit kind set (absent = every kind)."""

    chosen = getattr(args, "candidate_kind", None)
    if not chosen:
        return frozenset(ALL_CANDIDATE_KINDS)
    return frozenset(chosen)
