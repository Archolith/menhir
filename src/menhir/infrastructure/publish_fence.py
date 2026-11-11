"""Menhir side of the erasure publish fence (G-publish, G-fenced).

Plan: menhir-erasure-rebuild-plan-v2 sections 2.2 and 3.2; P4 design note, "Fence side".

``MenhirPublishFence`` implements the graphiti fork's ``PublishFence`` protocol
(``graphiti_core.publish_fence``): its two hooks run inside Graphiti's single publish transaction,
``before_write`` locks the namespace fence then the claimed queue node and re-checks everything in a
SEPARATE statement (so the re-read sees post-lock state), ``after_write`` links queue node and twin,
records ``READ_CONTEXT`` and runs the caller's extras (anchored-time persist, kept conflicts). Any
failed check raises ``PublishFenced`` and the transaction rolls back, so no twin exists.

``run_fenced`` / ``check_fence`` / ``requeue_fenced`` are the same checks for Menhir's own
post-claim writes. Every statement is idempotent (``SET`` / ``MERGE``): a managed transaction can be
re-run by the driver after a transient error.

``repair_turn_ids_provider`` is a zero-arg callable returning the ``TurnEvidence`` ids of the repair
context the extraction call actually read; the worker passes ``lambda: receipt.repair_turn_ids``
(``CombinedExtractionReceipt.repair_turn_ids``). It is called inside ``after_write``, after
extraction finished, so the list is complete by then.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from menhir.infrastructure.erasure_fence import (
    fence_lock_cypher,
    marker_absent_for_param_cypher,
)

logger = logging.getLogger(__name__)

__all__ = [
    "ClaimFence",
    "FenceLost",
    "GateFence",
    "MenhirPublishFence",
    "PublishFenced",
    "check_fence",
    "read_gate_fence",
    "requeue_fenced",
    "run_fenced",
]

#: An after_write extra: ``await extra(tx, episode_uuid=..., entity_edge_uuids=...)``. It runs its
#: statements through ``_tx_run`` on the publish transaction and must be idempotent.
AfterWriteExtra = Callable[..., Awaitable[None]]


@dataclass(frozen=True)
class ClaimFence:
    """The claim a worker holds: owner, claim instant, fence key and the reserved twin uuid."""

    q_uuid: str
    worker_id: str
    claim_started_at: Any
    namespace_key: str
    reserved_twin_uuid: str


@dataclass(frozen=True)
class GateFence:
    """The namespace ``erasure_epoch`` read right after gate entry."""

    epoch: int


class PublishFenced(Exception):
    """The publish transaction was vetoed; ``reason`` names the failed check."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class FenceLost(Exception):
    """A post-claim write was refused; ``reason`` names the failed check."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# --------------------------------------------------------------------------- statements
_INSTANT_OK = (
    "(q.processing_started_at IS NOT NULL AND $claim_started_at IS NOT NULL"
    " AND q.processing_started_at.epochSeconds = datetime($claim_started_at).epochSeconds"
    " AND q.processing_started_at.nanosecond = datetime($claim_started_at).nanosecond)"
)

#: Statement 1: lock the fence for the claim's namespace, then the claimed queue node.
LOCK_CYPHER = (
    fence_lock_cypher("$namespace_key")
    + " WITH f OPTIONAL MATCH (q:Episodic {uuid: $q_uuid})"
    " FOREACH (_ IN CASE WHEN q IS NULL THEN [] ELSE [1] END |"
    " SET q.lock_nonce = $fence_nonce, q.locked_at = datetime())"
    " RETURN q IS NOT NULL AS q_found"
)

#: Statement 2: re-read under the locks. The decision is taken in Python (``_failed_check``).
RECHECK_CYPHER = (
    # OPTIONAL: check_fence runs without the lock statement, so the fence may not exist yet.
    "OPTIONAL MATCH (f:EvidenceNamespaceFence {namespace_key: $namespace_key}) "
    "OPTIONAL MATCH (q:Episodic {uuid: $q_uuid}) "
    "RETURN q IS NOT NULL AS q_found, "
    "q.processing_state AS state, "
    "q.processing_owner AS owner, "
    f"{_INSTANT_OK} AS instant_ok, "
    f"{marker_absent_for_param_cypher('q_uuid')} AS no_marker, "
    "coalesce(f.erasure_epoch, 0) AS epoch, "
    "size(coalesce(f.erasing_ops, [])) AS erasing_ops, "
    "q.resolved_episode_uuid AS resolved, "
    "coalesce(q.reserved_twin_uuids, []) AS reserved"
)

LINK_CYPHER = (
    "MATCH (q:Episodic {uuid: $q_uuid}) MATCH (g:Episodic {uuid: $episode_uuid}) "
    "SET q.resolved_episode_uuid = $episode_uuid, g.menhir_memory_uuid = q.uuid "
    "RETURN count(g) AS linked"
)

READ_CONTEXT_EPISODES_CYPHER = (
    "MATCH (g:Episodic {uuid: $episode_uuid}) "
    "UNWIND $targets AS target_uuid "
    "MATCH (p:Episodic {uuid: target_uuid}) WHERE p.uuid <> g.uuid "
    "MERGE (g)-[r:READ_CONTEXT {kind: 'previous_episode'}]->(p) "
    "ON CREATE SET r.created_at = datetime() "
    "SET r.inferred = false "
    "RETURN count(r) AS written"
)

READ_CONTEXT_TURNS_CYPHER = (
    "MATCH (g:Episodic {uuid: $episode_uuid}) "
    "UNWIND $targets AS target_turn_id "
    "MATCH (t:TurnEvidence {turn_id: target_turn_id}) "
    "MERGE (g)-[r:READ_CONTEXT {kind: 'repair_turn'}]->(t) "
    "ON CREATE SET r.created_at = datetime() "
    "SET r.inferred = false "
    "RETURN count(r) AS written"
)

GATE_CYPHER = (
    "OPTIONAL MATCH (f:EvidenceNamespaceFence {namespace_key: $namespace_key}) "
    "RETURN coalesce(f.erasure_epoch, 0) AS epoch"
)

REQUEUE_CYPHER = (
    "MATCH (q:Episodic {uuid: $q_uuid}) "
    "WHERE q.processing_state = 'ENRICHING' AND q.processing_owner = $worker_id "
    f"AND {_INSTANT_OK} AND {marker_absent_for_param_cypher('q_uuid')} "
    # CF-324: back off 5s * 2^n, capped at 10 min; a fence veto never fails the episode.
    "WITH q, coalesce(toInteger(q.fenced_requeues), 0) AS n "
    "SET q.fenced_requeues = n + 1, q.processing_attempts = CASE WHEN coalesce(toInteger(q.processing_attempts), 0) > 0 "
    "THEN toInteger(q.processing_attempts) - 1 ELSE 0 END, "
    "q.processing_state = 'PENDING', q.processing_stage = 'queued', "
    "q.processing_substage = 'publish_fenced', q.processing_substage_started_at = datetime(), "
    "q.processing_owner = null, q.processing_lease_expires_at = null, "
    "q.processing_heartbeat_at = datetime(), q.processing_error = null, "
    "q.processing_llm_active_task = null, q.processing_llm_active_kind = null, "
    "q.processing_llm_active_model = null, q.processing_llm_active_endpoint = null, "
    "q.retry_after = datetime() + duration({seconds: CASE WHEN n >= 7 THEN 600 "
    "ELSE toInteger(5 * 2 ^ n) END}) "
    "RETURN count(q) AS requeued"
)


# --------------------------------------------------------------------------- helpers
async def _tx_run(
    tx: Any, query: str, params: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """Run ``query`` on either transaction flavour and return plain row dicts.

    * Menhir's sync ``Neo4jTransaction``: ``tx.execute(query, params)``.
    * The fork's async neo4j transaction: ``await tx.run(query, **params)`` then
      ``await result.data()``.
    """
    params = params or {}
    execute = getattr(tx, "execute", None)
    if callable(execute):
        return [dict(row) for row in execute(query, params)]
    result = tx.run(query, **params)
    if inspect.isawaitable(result):
        result = await result
    data = result.data()
    if inspect.isawaitable(data):
        data = await data
    return [dict(row) for row in data]


def _claim_params(claim: ClaimFence) -> dict[str, Any]:
    return {
        "q_uuid": claim.q_uuid,
        "worker_id": claim.worker_id,
        "claim_started_at": claim.claim_started_at,
        "namespace_key": claim.namespace_key,
    }


def _lock_params(claim: ClaimFence) -> dict[str, Any]:
    return {**_claim_params(claim), "fence_nonce": uuid4().hex}


def _failed_check(
    row: dict[str, Any] | None,
    claim: ClaimFence,
    gate: GateFence | None,
    *,
    episode_uuid: str | None = None,
    publish: bool = False,
) -> str | None:
    """The name of the first failed check, or None. Pure."""
    if row is None or not row.get("q_found"):
        return "queue_node_missing"
    if row.get("state") != "ENRICHING":
        return "not_enriching"
    if row.get("owner") != claim.worker_id:
        return "owner_changed"
    if not row.get("instant_ok"):
        return "claim_instant_changed"
    if not row.get("no_marker"):
        return "erasure_marker"
    if gate is not None:
        if int(row.get("epoch") or 0) != int(gate.epoch):
            return "epoch_moved"
        if int(row.get("erasing_ops") or 0) > 0:
            return "erasing_ops"
    if publish:
        if row.get("resolved") is not None:
            return "already_linked"
        reserved = list(row.get("reserved") or [])
        if episode_uuid != claim.reserved_twin_uuid or episode_uuid not in reserved:
            return "uuid_not_reserved"
    return None


def read_gate_fence(neo4j: Any, namespace_key: str) -> GateFence:
    """The namespace's current ``erasure_epoch`` (0 when it has no fence yet)."""
    rows = neo4j.execute(GATE_CYPHER, params={"namespace_key": namespace_key})
    epoch = rows[0].get("epoch") if rows else 0
    return GateFence(epoch=int(epoch or 0))


# --------------------------------------------------------------------------- publish fence
class MenhirPublishFence:
    """G-publish: the fork's ``PublishFence`` implementation (see module docstring)."""

    def __init__(
        self,
        claim: ClaimFence,
        gate: GateFence,
        *,
        after_write_extras: Sequence[AfterWriteExtra] = (),
        repair_turn_ids_provider: Callable[[], Iterable[str]] | None = None,
    ) -> None:
        self.claim = claim
        self.gate = gate
        self.after_write_extras = tuple(after_write_extras)
        self.repair_turn_ids_provider = repair_turn_ids_provider

    def with_extras(self, *extras: AfterWriteExtra) -> MenhirPublishFence:
        """A copy with ``extras`` appended; the original is not mutated."""
        return MenhirPublishFence(
            self.claim,
            self.gate,
            after_write_extras=(*self.after_write_extras, *extras),
            repair_turn_ids_provider=self.repair_turn_ids_provider,
        )

    async def before_write(
        self,
        tx: Any,
        *,
        episode_uuid: str,
        previous_episode_uuids: Sequence[str],
    ) -> None:
        claim = self.claim
        await _tx_run(tx, LOCK_CYPHER, _lock_params(claim))
        rows = await _tx_run(tx, RECHECK_CYPHER, _claim_params(claim))
        reason = _failed_check(
            rows[0] if rows else None,
            claim,
            self.gate,
            episode_uuid=episode_uuid,
            publish=True,
        )
        if reason is not None:
            raise PublishFenced(reason)

    async def after_write(
        self,
        tx: Any,
        *,
        episode_uuid: str,
        entity_node_uuids: Sequence[str],
        entity_edge_uuids: Sequence[str],
        previous_episode_uuids: Sequence[str],
    ) -> None:
        linked = await _tx_run(
            tx, LINK_CYPHER, {"q_uuid": self.claim.q_uuid, "episode_uuid": episode_uuid}
        )
        if not linked or not int(linked[0].get("linked") or 0):
            raise PublishFenced("link_target_missing")
        previous = sorted({str(u) for u in previous_episode_uuids if u})
        if previous:
            await _tx_run(
                tx,
                READ_CONTEXT_EPISODES_CYPHER,
                {"episode_uuid": episode_uuid, "targets": previous},
            )
        provider = self.repair_turn_ids_provider
        repair = sorted(
            {str(t) for t in (provider() if provider is not None else ()) if t}
        )
        if repair:
            await _tx_run(
                tx,
                READ_CONTEXT_TURNS_CYPHER,
                {"episode_uuid": episode_uuid, "targets": repair},
            )
        for extra in self.after_write_extras:
            await extra(
                tx, episode_uuid=episode_uuid, entity_edge_uuids=list(entity_edge_uuids)
            )


# --------------------------------------------------------------------------- G-fenced
def run_fenced(
    neo4j: Any,
    claim: ClaimFence,
    gate: GateFence | None,
    statements: Iterable[tuple[str, dict[str, Any]]],
) -> list[list[dict[str, Any]]]:
    """Run ``statements`` in one write transaction after locking and re-checking the claim.

    Same checks as G-publish minus the resolved/reservation ones; the epoch and ``erasing_ops``
    checks apply only when ``gate`` is given (writes before gate entry carry no foreign reads).
    Raises ``FenceLost``; nothing is written then. Returns each statement's rows.
    """
    prepared = [(query, dict(params or {})) for query, params in statements]

    def work(tx: Any) -> list[list[dict[str, Any]]]:
        tx.execute(LOCK_CYPHER, _lock_params(claim))
        rows = tx.execute(RECHECK_CYPHER, _claim_params(claim))
        reason = _failed_check(rows[0] if rows else None, claim, gate)
        if reason is not None:
            raise FenceLost(reason)
        return [list(tx.execute(query, params)) for query, params in prepared]

    return neo4j.execute_write(work)


def check_fence(neo4j: Any, claim: ClaimFence, gate: GateFence | None) -> None:
    """Read-only G-fenced check for steps that cannot join one transaction. Raises ``FenceLost``."""
    rows = neo4j.execute(RECHECK_CYPHER, params=_claim_params(claim))
    reason = _failed_check(rows[0] if rows else None, claim, gate)
    if reason is not None:
        raise FenceLost(reason)


def requeue_fenced(neo4j: Any, claim: ClaimFence, reason: str = "") -> bool:
    """Undo a fenced attempt: back to PENDING, attempt refunded, retry after 5s * 2^n.

    n counts prior fenced requeues (``q.fenced_requeues``); the delay is capped at 10 min.

    Writes only while the claim is still ours and unmarked. With an erase marker on the queue node
    nothing is written (the erased memory must not be touched) and False is returned. Logs a
    ``publish_fenced`` lifecycle event either way.
    """

    def work(tx: Any) -> bool:
        rows = tx.execute(REQUEUE_CYPHER, _claim_params(claim))
        return bool(rows and int(rows[0].get("requeued") or 0) > 0)

    requeued = neo4j.execute_write(work)
    _log_publish_fenced(claim, reason, "requeued" if requeued else "silent")
    return requeued


def _log_publish_fenced(claim: ClaimFence, reason: str, state: str) -> None:
    logger.info(
        "publish_fenced episode=%s reason=%s outcome=%s", claim.q_uuid, reason, state
    )
    try:
        from menhir.infrastructure.telemetry import record_lifecycle_event

        record_lifecycle_event(
            component="publish_fence",
            event="publish_fenced",
            state=state,
            episode_uuid=claim.q_uuid,
            details={"reason": reason, "worker_id": claim.worker_id},
        )
    except Exception:  # telemetry is advisory
        logger.debug("publish_fenced telemetry failed", exc_info=True)
