"""Erasure fence primitives shared by the erase seal and the publish fence.

Plan: menhir-erasure-rebuild-plan-v2 sections 2.1-2.2; P4 design note. Every writer that must see an erase
uses these exact expressions, so the namespace key and the marker predicate cannot drift apart.
"""

from __future__ import annotations

import logging
import uuid as uuidlib
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

#: Label of the permanent per-uuid erase marker.
ERASURE_MARKER_LABEL = "ErasureMarker"


def fence_key_cypher(var: str) -> str:
    """The ``EvidenceNamespaceFence.namespace_key`` of node ``var``.

    Same mapping as the single-memory cascade (``memory_queries.py``): ``namespace`` wins over
    ``group_id``, and blank or missing maps to ``'default'`` (physical group_id ``''``).
    """
    raw = f"trim(toString(coalesce({var}.namespace, {var}.group_id, '')))"
    return f"CASE WHEN {raw} = '' THEN 'default' ELSE {raw} END"


def marker_absent_cypher(var: str) -> str:
    """G-q: true when no erase marker exists for ``var.uuid``."""
    return f"NOT EXISTS {{ MATCH (:{ERASURE_MARKER_LABEL} {{subject_uuid: {var}.uuid}}) }}"


def marker_absent_for_param_cypher(param: str) -> str:
    """G-q for a uuid passed as query parameter ``$param``."""
    return f"NOT EXISTS {{ MATCH (:{ERASURE_MARKER_LABEL} {{subject_uuid: ${param}}}) }}"


#: Lock idiom for one fence key ``$<param>``; the SET takes the node write lock.
def fence_lock_cypher(
    key_expr: str, *, var: str = "f", nonce_param: str = "fence_nonce"
) -> str:
    return (
        f"MERGE ({var}:EvidenceNamespaceFence {{namespace_key: {key_expr}}}) "
        f"ON CREATE SET {var}.generation = 0, {var}.created_at = datetime(), {var}.erasure_epoch = 0 "
        f"SET {var}.lock_nonce = ${nonce_param}, {var}.locked_at = datetime()"
    )


# ---------------------------------------------------------------------------------------------
# Seal and release (erase side). P4 design note, "Seal and release".
# ---------------------------------------------------------------------------------------------
@dataclass
class SealResult:
    """What a seal locked and what it found.

    ``closure_uuids`` always contains the existing targets themselves plus everything reachable
    from them in the locked namespaces; ``unfenced_inflight`` lists closure queue nodes that are
    ENRICHING under a claim that cannot honour the publish fence.
    """

    namespace_keys: list[str] = field(default_factory=list)
    closure_uuids: list[str] = field(default_factory=list)
    unfenced_inflight: list[str] = field(default_factory=list)
    sealed_epoch: int = 0


def _fill(template: str, **vars_: str) -> str:
    out = template
    for name, cypher in vars_.items():
        out = out.replace(f"@{name}@", cypher)
    return out


_BUMP_AND_APPEND = (
    "SET f.erasure_epoch = coalesce(f.erasure_epoch, 0) + 1, "
    "f.erasing_ops = CASE WHEN $op_id IN coalesce(f.erasing_ops, []) "
    "THEN f.erasing_ops ELSE coalesce(f.erasing_ops, []) + [$op_id] END "
    "RETURN f.namespace_key AS key, f.erasure_epoch AS epoch"
)

#: Step 1: the fence key of every target (pre-read, before any lock).
_TARGET_KEYS_CYPHER = _fill(
    """
    MATCH (n:Episodic) WHERE n.uuid IN $targets RETURN @KN@ AS key, n.uuid AS uuid
    UNION
    MATCH (n:Entity) WHERE n.uuid IN $targets RETURN @KN@ AS key, n.uuid AS uuid
    UNION
    MATCH (n:TurnEvidence) WHERE n.turn_id IN $targets RETURN @KN@ AS key, n.turn_id AS uuid
    """,
    KN=fence_key_cypher("n"),
)

#: Step 3a: lock the target queue nodes (the targets, and the queue node of any twin target).
_LOCK_QUEUE_NODES_CYPHER = _fill(
    """
    MATCH (t:Episodic) WHERE t.uuid IN $targets AND @KT@ IN $keys
    OPTIONAL MATCH (b1:Episodic)
    WHERE b1.uuid = t.menhir_memory_uuid AND @KB1@ IN $keys
    OPTIONAL MATCH (b2:Episodic)
    WHERE t.processing_state IS NULL AND b2.resolved_episode_uuid = t.uuid AND @KB2@ IN $keys
    WITH collect(DISTINCT t) + collect(DISTINCT b1) + collect(DISTINCT b2) AS cands
    UNWIND cands AS c
    WITH DISTINCT c WHERE c.processing_state IS NOT NULL
    SET c.lock_nonce = $nonce
    RETURN collect(c.uuid) AS qs
    """,
    KT=fence_key_cypher("t"),
    KB1=fence_key_cypher("b1"),
    KB2=fence_key_cypher("b2"),
)

#: Step 3b: the closure, read under the locks taken above. Queue nodes, their ADMITTED_ON turns, the
#: turns' evidence projections, and for every queue node and projection its link, reserved twins and
#: back-linked twins. Only nodes whose fence key is one of the locked keys are followed.
SEAL_CLOSURE_CYPHER = _fill(
    """
    MATCH (q:Episodic) WHERE q.uuid IN $qs AND @KQ@ IN $keys
    WITH collect(DISTINCT q) AS qnodes
    CALL {
        WITH qnodes
        UNWIND qnodes AS q
        OPTIONAL MATCH (q)-[:ADMITTED_ON]->(turn:TurnEvidence)
        WHERE @KT@ IN $keys
        RETURN collect(DISTINCT turn) AS turns
    }
    OPTIONAL MATCH (proj:Episodic)
    WHERE proj.evidence_projection_of IN [t IN turns | t.turn_id] AND @KP@ IN $keys
    WITH qnodes, turns, collect(DISTINCT proj) AS projs
    WITH qnodes, turns, projs, qnodes + projs AS bases
    CALL {
        WITH bases
        UNWIND bases AS b
        OPTIONAL MATCH (tw:Episodic)
        WHERE tw.menhir_memory_uuid = b.uuid AND @KTW@ IN $keys
        WITH b, collect(DISTINCT tw.uuid) AS backlinked
        RETURN collect({
            key: @KB@,
            ids: [x IN [b.resolved_episode_uuid] + coalesce(b.reserved_twin_uuids, []) + backlinked
                  WHERE x IS NOT NULL]
        }) AS twin_groups
    }
    RETURN [q IN qnodes | {uuid: q.uuid, key: @KQ2@}] AS queues,
           [t IN turns | {uuid: t.turn_id, key: @KT2@}] AS turns,
           [p IN projs | {uuid: p.uuid, key: @KP2@}] AS projections,
           twin_groups,
           [x IN qnodes + projs
            WHERE x.processing_state = 'ENRICHING'
              AND NOT (coalesce(x.publish_protocol, 0) = 2
                       AND x.publish_protocol_claimed_at IS NOT NULL
                       AND x.publish_protocol_claimed_at = x.processing_started_at)
            | x.uuid] AS unfenced
    """,
    KQ=fence_key_cypher("q"),
    KT=fence_key_cypher("turn"),
    KP=fence_key_cypher("proj"),
    KTW=fence_key_cypher("tw"),
    KB=fence_key_cypher("b"),
    KQ2=fence_key_cypher("q"),
    KT2=fence_key_cypher("t"),
    KP2=fence_key_cypher("p"),
)

#: Link/reserved uuids that exist as nodes in a namespace we did not lock are not ours to mark.
_FOREIGN_CYPHER = _fill(
    "MATCH (n:Episodic) WHERE n.uuid IN $ids AND NOT @KN@ IN $keys RETURN DISTINCT n.uuid AS uuid",
    KN=fence_key_cypher("n"),
)

_MARKER_CYPHER = (
    f"UNWIND $items AS it "
    f"MERGE (m:{ERASURE_MARKER_LABEL} {{namespace_key: it.key, subject_uuid: it.uuid, op_id: $op_id}}) "
    f"ON CREATE SET m.sealed_at = datetime()"
)

_PRIMARY_MARKER_CYPHER = (
    f"MATCH (m:{ERASURE_MARKER_LABEL} {{op_id: $op_id}}) WHERE m.subject_uuid IN $targets "
    f"SET m.sealed_epoch = $epoch, m.subject_uuids = $subjects"
)


def _lock_and_bump(tx: Any, keys: list[str], op_id: str, nonce: str) -> int:
    """Lock the fences in sorted key order, bump ``erasure_epoch`` and append ``op_id``."""
    epoch = 0
    for key in sorted(keys):
        rows = tx.execute(
            fence_lock_cypher("$key") + " " + _BUMP_AND_APPEND,
            {"key": key, "fence_nonce": nonce, "op_id": op_id},
        )
        for row in rows:
            epoch = max(epoch, int(row.get("epoch") or 0))
    return epoch


def seal_memory(
    neo4j: Any,
    op_id: str,
    target_uuids: list[str] | tuple[str, ...],
    *,
    caller_namespace: str | None = None,
) -> SealResult:
    """Seal the namespaces of ``target_uuids`` and mark the erase closure, in one transaction.

    With ``caller_namespace`` set, a target whose fence key is another namespace raises
    ``PermissionError`` before any fence is locked (CF-326, defense in depth).
    """
    targets = [str(u) for u in dict.fromkeys(target_uuids) if u]
    scope = (caller_namespace or "").strip()
    scope_key = scope

    def work(tx: Any) -> SealResult:
        nonce = uuidlib.uuid4().hex
        # 1. pre-read the fence keys of the targets, before any lock.
        found = tx.execute(_TARGET_KEYS_CYPHER, {"targets": targets})
        keys = sorted({str(r["key"]) for r in found if r.get("key")})
        if not keys:
            return SealResult()
        if scope_key and keys != [scope_key]:
            raise PermissionError(
                f"seal targets resolve to fence keys {keys}, outside {scope!r}; refusing"
            )
        target_key: dict[str, str] = {}
        for r in found:
            target_key.setdefault(str(r["uuid"]), str(r["key"]))
        # 2. lock the fences in sorted order; epoch + op id.
        epoch = _lock_and_bump(tx, keys, op_id, nonce)
        # 3. lock the target queue nodes (separate statement), then read the closure under lock.
        locked = tx.execute(
            _LOCK_QUEUE_NODES_CYPHER, {"targets": targets, "keys": keys, "nonce": nonce}
        )
        qs = [str(u) for u in (locked[0].get("qs") if locked else []) or []]
        rows = tx.execute(SEAL_CLOSURE_CYPHER, {"qs": qs, "keys": keys})
        row = rows[0] if rows else {}
        items: dict[str, str] = dict(target_key)
        for group in ("queues", "turns", "projections"):
            for entry in row.get(group) or []:
                if entry.get("uuid"):
                    items.setdefault(str(entry["uuid"]), str(entry["key"]))
        linked: dict[str, str] = {}
        for grp in row.get("twin_groups") or []:
            for u in grp.get("ids") or []:
                linked.setdefault(str(u), str(grp["key"]))
        if linked:
            foreign = {
                str(r["uuid"])
                for r in tx.execute(
                    _FOREIGN_CYPHER, {"ids": sorted(linked), "keys": keys}
                )
            }
            for u, k in linked.items():
                if u not in foreign:
                    items.setdefault(u, k)
        # 4. ENRICHING claims that cannot honour the publish fence.
        unfenced = sorted({str(u) for u in row.get("unfenced") or []})
        # 5. one marker per closure uuid; the primary fields on the targets' markers.
        tx.execute(
            _MARKER_CYPHER,
            {
                "items": [{"key": k, "uuid": u} for u, k in items.items()],
                "op_id": op_id,
            },
        )
        subjects = sorted(target_key)
        tx.execute(
            _PRIMARY_MARKER_CYPHER,
            {"op_id": op_id, "targets": subjects, "epoch": epoch, "subjects": subjects},
        )
        return SealResult(
            namespace_keys=keys,
            closure_uuids=list(items),
            unfenced_inflight=unfenced,
            sealed_epoch=epoch,
        )

    return neo4j.execute_write(work)


def seal_namespace(neo4j: Any, op_id: str, namespace: str) -> SealResult:
    """Seal one namespace for a namespace erase: epoch bump plus ``erasing_ops`` (no per-member markers)."""
    key = str(namespace or "").strip() or "default"

    def work(tx: Any) -> SealResult:
        epoch = _lock_and_bump(tx, [key], op_id, uuidlib.uuid4().hex)
        return SealResult(namespace_keys=[key], sealed_epoch=epoch)

    return neo4j.execute_write(work)


def release_memory(neo4j: Any, op_id: str) -> list[str]:
    """Remove ``op_id`` from every fence's ``erasing_ops`` and bump the epoch. Idempotent.

    Returns the namespace keys released (empty when the op held nothing).
    """

    def work(tx: Any) -> list[str]:
        nonce = uuidlib.uuid4().hex
        held = tx.execute(
            "MATCH (f:EvidenceNamespaceFence) WHERE $op_id IN coalesce(f.erasing_ops, []) "
            "RETURN f.namespace_key AS key",
            {"op_id": op_id},
        )
        released: list[str] = []
        epoch = 0
        for key in sorted({str(r["key"]) for r in held if r.get("key") is not None}):
            rows = tx.execute(
                "MATCH (f:EvidenceNamespaceFence {namespace_key: $key}) "
                "WHERE $op_id IN coalesce(f.erasing_ops, []) "
                "SET f.lock_nonce = $nonce, f.locked_at = datetime(), "
                "f.erasing_ops = [o IN f.erasing_ops WHERE o <> $op_id], "
                "f.erasure_epoch = coalesce(f.erasure_epoch, 0) + 1 "
                "RETURN f.namespace_key AS key, f.erasure_epoch AS epoch",
                {"key": key, "op_id": op_id, "nonce": nonce},
            )
            for row in rows:
                released.append(str(row["key"]))
                epoch = max(epoch, int(row.get("epoch") or 0))
        if released:
            tx.execute(
                f"MATCH (m:{ERASURE_MARKER_LABEL} {{op_id: $op_id}}) "
                "WHERE m.sealed_epoch IS NOT NULL "
                "SET m.released_epoch = $epoch, m.cascade_done = true",
                {"op_id": op_id, "epoch": epoch},
            )
        return released

    return neo4j.execute_write(work)


release_namespace = release_memory


#: An op absent from this sidecar's journal may belong to another sidecar mid-erase (B-8); it is
#: released only once its markers are older than this.
ORPHAN_ABSENT_GRACE_S = 3600.0

_OP_MARKER_AGE_CYPHER = (
    f"OPTIONAL MATCH (m:{ERASURE_MARKER_LABEL} {{op_id: $op_id}}) "
    "RETURN count(m) AS markers, "
    "min(duration.inSeconds(m.sealed_at, datetime()).seconds) AS age_s"
)


def release_orphaned_erasing_ops(
    neo4j: Any, journal: Any, *, absent_grace_s: float = ORPHAN_ABSENT_GRACE_S
) -> list[str]:
    """Release ``erasing_ops`` entries whose journal row is terminal, or absent past the grace.

    A PREPARED row is left alone: its replay releases it. An absent row is released only when the
    op's markers were sealed more than ``absent_grace_s`` ago; with no markers (a namespace seal)
    it is held and logged. Returns the op ids released.
    """
    rows = neo4j.execute(
        "MATCH (f:EvidenceNamespaceFence) UNWIND coalesce(f.erasing_ops, []) AS op "
        "RETURN DISTINCT op"
    )
    released: list[str] = []
    for op_id in sorted({str(r["op"]) for r in rows if r.get("op")}):
        row = journal.get(op_id)
        if row is not None and str(row.get("state") or "") == "PREPARED":
            continue
        if row is None:
            age = neo4j.execute(_OP_MARKER_AGE_CYPHER, params={"op_id": op_id})
            markers = int(age[0].get("markers") or 0) if age else 0
            age_s = age[0].get("age_s") if age else None
            if not markers or age_s is None or float(age_s) < absent_grace_s:
                logger.warning(
                    "erasing op %s has no journal row here; held (markers=%d, age_s=%s)",
                    op_id,
                    markers,
                    age_s,
                )
                continue
        release_memory(neo4j, op_id)
        released.append(op_id)
        logger.warning(
            "released orphaned erasing op %s (journal state %s)",
            op_id,
            row.get("state") if row else "absent",
        )
    return released
