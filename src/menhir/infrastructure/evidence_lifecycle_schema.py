"""Bring the evidence-lifecycle uniqueness constraints online at every start (#603).

The erase seal, the publish fence and every other fence writer find a namespace's fence with
``MERGE (f:EvidenceNamespaceFence {namespace_key: ...})``. MERGE is atomic against concurrent
creators only when a uniqueness constraint backs it; without one, two first writers in a new
namespace can each create a fence and lock different nodes. The same holds for the publication
intent, tombstone and projection-repair keys.

Order: merge duplicate fences, refuse duplicates on the other labels, create the constraints,
then re-read them. Startup refuses writers until all four are online.
"""

from __future__ import annotations

import logging
import uuid as uuidlib
from typing import Any

from menhir.infrastructure.erasure_fence import FENCE_LABEL
from menhir.infrastructure.schema import get_view_evidence_lifecycle_activation_queries

logger = logging.getLogger(__name__)

#: Label a merged-away fence copy is moved to. Kept, not deleted: the erasure rules forbid
#: deleting a fence, and the copy is the before-image of the merge.
DUPLICATE_FENCE_LABEL = "EvidenceNamespaceFenceDuplicate"

#: Name, constraint type, entity type, labels, properties (the shape SHOW CONSTRAINTS reports).
EVIDENCE_LIFECYCLE_REQUIRED_CONSTRAINTS = (
    ("evidence_namespace_fence_namespace_unique", "UNIQUENESS", "NODE",
     (FENCE_LABEL,), ("namespace_key",)),
    ("evidence_publication_intent_key_unique", "UNIQUENESS", "NODE",
     ("EvidencePublicationIntent",), ("intent_key",)),
    ("evidence_tombstone_key_unique", "UNIQUENESS", "NODE",
     ("EvidenceTombstone",), ("tombstone_key",)),
    ("view_projection_repair_key_unique", "UNIQUENESS", "NODE",
     ("ViewProjectionRepair",), ("repair_key",)),
)

#: Labels whose duplicates are refused rather than merged. Each key embeds a unique operation
#: id, so a duplicate needs two transactions with one operation id; no merge rule is assumed.
_REFUSED_DUPLICATE_KEYS = (
    ("EvidencePublicationIntent", "intent_key"),
    ("EvidenceTombstone", "tombstone_key"),
    ("ViewProjectionRepair", "repair_key"),
)

_MAX_ATTEMPTS = 3


class EvidenceLifecycleSchemaError(RuntimeError):
    """The evidence-lifecycle constraints could not be brought online; writers must not start."""


_DUPLICATE_FENCE_KEYS_CYPHER = (
    f"MATCH (f:{FENCE_LABEL}) WHERE f.namespace_key IS NOT NULL "
    "WITH f.namespace_key AS key, count(*) AS copies WHERE copies > 1 "
    "RETURN key ORDER BY key"
)

#: Lock every copy (concurrent lockers MERGE-match all of them, so they wait), then pick the
#: survivor: highest erasure_epoch, then oldest.
_LOCK_COPIES_CYPHER = (
    f"MATCH (f:{FENCE_LABEL} {{namespace_key: $key}}) "
    "SET f.lock_nonce = $nonce, f.locked_at = datetime() "
    "WITH f ORDER BY coalesce(f.erasure_epoch, 0) DESC, f.created_at ASC, elementId(f) ASC "
    "WITH collect(f) AS fs "
    "RETURN elementId(head(fs)) AS keep_id, [d IN tail(fs) | elementId(d)] AS dup_ids"
)

_FOREIGN_RELS_CYPHER = (
    "MATCH (d)-[r]-() WHERE elementId(d) IN $dup_ids AND type(r) <> 'GOVERNS_PUBLICATION' "
    "RETURN collect(DISTINCT type(r)) AS types"
)

#: Survivor takes the highest epoch and generation, every in-flight erase op and the oldest
#: created_at: readers then see an epoch at least as high as any copy they recorded.
_MERGE_INTO_SURVIVOR_CYPHER = (
    "MATCH (keep) WHERE elementId(keep) = $keep_id "
    "MATCH (d) WHERE elementId(d) IN $dup_ids "
    "WITH keep, collect(d) AS ds "
    "WITH keep, ds, [keep] + ds AS all "
    "SET keep.erasure_epoch = reduce(m = 0, x IN all | "
    "        CASE WHEN coalesce(x.erasure_epoch, 0) > m THEN coalesce(x.erasure_epoch, 0) ELSE m END), "
    "    keep.generation = reduce(m = 0, x IN all | "
    "        CASE WHEN coalesce(x.generation, 0) > m THEN coalesce(x.generation, 0) ELSE m END), "
    "    keep.erasing_ops = reduce(acc = [], o IN reduce(l = [], x IN all | "
    "        l + coalesce(x.erasing_ops, [])) | CASE WHEN o IN acc THEN acc ELSE acc + [o] END), "
    "    keep.created_at = reduce(m = keep.created_at, x IN all | "
    "        CASE WHEN m IS NULL OR (x.created_at IS NOT NULL AND x.created_at < m) "
    "        THEN x.created_at ELSE m END) "
    "RETURN keep.erasure_epoch AS epoch"
)

_REPOINT_PUBLICATIONS_CYPHER = (
    "MATCH (keep) WHERE elementId(keep) = $keep_id "
    "MATCH (d)-[r:GOVERNS_PUBLICATION]->(i) WHERE elementId(d) IN $dup_ids "
    "MERGE (keep)-[:GOVERNS_PUBLICATION]->(i) "
    "DELETE r"
)

_RELABEL_DUPLICATES_CYPHER = (
    f"MATCH (d:{FENCE_LABEL}) WHERE elementId(d) IN $dup_ids "
    f"REMOVE d:{FENCE_LABEL} "
    f"SET d:{DUPLICATE_FENCE_LABEL}, d.duplicate_of_key = d.namespace_key, "
    "d.deduplicated_at = datetime() "
    "RETURN count(d) AS moved"
)

_SHOW_CONSTRAINTS_CYPHER = (
    "SHOW CONSTRAINTS YIELD name, type, entityType, labelsOrTypes, properties "
    "WHERE name IN $names RETURN name, type, entityType, labelsOrTypes, properties"
)


def evidence_lifecycle_schema_ready(neo4j: Any) -> bool:
    """True when every required constraint exists with the expected shape."""
    expected = {name: rest for name, *rest in EVIDENCE_LIFECYCLE_REQUIRED_CONSTRAINTS}
    rows = neo4j.execute(_SHOW_CONSTRAINTS_CYPHER, params={"names": list(expected)})
    actual = {
        str(row.get("name") or ""): [
            str(row.get("type") or ""),
            str(row.get("entityType") or ""),
            tuple(str(v) for v in (row.get("labelsOrTypes") or [])),
            tuple(str(v) for v in (row.get("properties") or [])),
        ]
        for row in rows
    }
    return all(actual.get(name) == list(shape) for name, shape in expected.items())


def _merge_one_fence_key(neo4j: Any, key: str) -> dict[str, Any]:
    def work(tx: Any) -> dict[str, Any]:
        rows = tx.execute(_LOCK_COPIES_CYPHER, {"key": key, "nonce": uuidlib.uuid4().hex})
        if not rows or not rows[0].get("dup_ids"):
            return {"key": key, "moved": 0}
        keep_id, dup_ids = rows[0]["keep_id"], list(rows[0]["dup_ids"])
        foreign = tx.execute(_FOREIGN_RELS_CYPHER, {"dup_ids": dup_ids})
        types = list(foreign[0].get("types") or []) if foreign else []
        if types:
            raise EvidenceLifecycleSchemaError(
                f"duplicate fences for namespace {key!r} carry relationships {types}; "
                "refusing to merge them automatically"
            )
        merged = tx.execute(_MERGE_INTO_SURVIVOR_CYPHER, {"keep_id": keep_id, "dup_ids": dup_ids})
        tx.execute(_REPOINT_PUBLICATIONS_CYPHER, {"keep_id": keep_id, "dup_ids": dup_ids})
        moved = tx.execute(_RELABEL_DUPLICATES_CYPHER, {"dup_ids": dup_ids})
        return {
            "key": key,
            "moved": int(moved[0].get("moved") or 0) if moved else 0,
            "epoch": int(merged[0].get("epoch") or 0) if merged else 0,
        }

    return neo4j.execute_write(work)


def merge_duplicate_fences(neo4j: Any) -> list[dict[str, Any]]:
    """Merge every namespace's duplicate fence copies into one; return one row per merged key."""
    keys = [str(r["key"]) for r in neo4j.execute(_DUPLICATE_FENCE_KEYS_CYPHER)]
    merged = [_merge_one_fence_key(neo4j, key) for key in keys]
    for row in merged:
        logger.warning(
            "Merged %d duplicate %s copies for namespace %r (epoch now %s); copies kept as :%s",
            row.get("moved", 0), FENCE_LABEL, row["key"], row.get("epoch"), DUPLICATE_FENCE_LABEL,
        )
    return merged


def refused_duplicates(neo4j: Any) -> dict[str, list[str]]:
    """Duplicate keys on labels whose duplicates are refused (sample of up to 10 per label)."""
    found: dict[str, list[str]] = {}
    for label, prop in _REFUSED_DUPLICATE_KEYS:
        rows = neo4j.execute(
            f"MATCH (n:{label}) WHERE n.{prop} IS NOT NULL "
            f"WITH n.{prop} AS key, count(*) AS copies WHERE copies > 1 "
            "RETURN collect(key)[..10] AS keys"
        )
        keys = [str(k) for k in (rows[0].get("keys") or [])] if rows else []
        if keys:
            found[label] = keys
    return found


def ensure_evidence_lifecycle_schema(neo4j: Any) -> dict[str, Any]:
    """Bring the constraints online or raise :class:`EvidenceLifecycleSchemaError`.

    Idempotent and cheap once online. Retried because a first writer can create a new duplicate
    fence between the merge and ``CREATE CONSTRAINT``, which then fails.
    """
    if evidence_lifecycle_schema_ready(neo4j):
        return {"status": "ok", "skipped": True, "merged_fences": [], "failures": []}
    merged_all: list[dict[str, Any]] = []
    failures: list[str] = []
    for _attempt in range(_MAX_ATTEMPTS):
        refused = refused_duplicates(neo4j)
        if refused:
            raise EvidenceLifecycleSchemaError(
                "duplicate evidence-lifecycle keys need operator review before the uniqueness "
                f"constraints can come online: {refused}"
            )
        merged_all.extend(merge_duplicate_fences(neo4j))
        failures = []
        for query in get_view_evidence_lifecycle_activation_queries():
            try:
                neo4j.execute(query)
            except Exception as exc:  # noqa: BLE001 - re-read below decides
                failures.append(str(exc))
        if evidence_lifecycle_schema_ready(neo4j):
            return {
                "status": "ok", "skipped": False, "merged_fences": merged_all,
                "failures": failures,
            }
    raise EvidenceLifecycleSchemaError(
        "evidence-lifecycle uniqueness constraints are not online after "
        f"{_MAX_ATTEMPTS} attempts; refusing writer startup. Failures: {'; '.join(failures)}"
    )
