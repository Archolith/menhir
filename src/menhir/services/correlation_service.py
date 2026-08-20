"""Semantic correlation service — routes high-similarity entity pairs.

When two entities have high cosine similarity but are not genuine
contradictions, the correlation service routes them appropriately
instead of pushing them into the conflict queue:

  similarity < 0.70  →  novel, store normally (no action)
0.70 – 0.85 → related but distinct, create RELATES_TO edge
  0.85 – 0.95        →  strong overlap, flag for LLM review (conflict path)
  > 0.95             →  near-duplicate, merge into survivor

This replaces the previous behaviour where *all* pairs above 0.85
were flagged as contradictions, flooding the conflict queue with
correlated-but-not-contradictory memories.

Thresholds are based on the audit of 11,412 entities and the
existing SIMILARITY_CONFLICT_THRESHOLD (0.85) and sharpness
threshold (0.7) in lifecycle_service.py.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, TYPE_CHECKING

from menhir.infrastructure.correlation_queries import CorrelationRepository
from menhir.infrastructure.telemetry import record_mcp_event

if TYPE_CHECKING:
    from menhir.infrastructure.llm import LLMAdapter

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

#: Below this threshold, no correlation action is taken.
CORRELATION_RELATED_THRESHOLD = 0.70

#: Above this threshold, pairs are flagged for LLM review (conflict path).
CORRELATION_CONFLICT_THRESHOLD = 0.85

#: Above this threshold, pairs are considered near-duplicates and merged.
CORRELATION_MERGE_THRESHOLD = 0.95


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class CorrelationResult:
    """Outcome of a single correlation check."""

    action: str  # "none" | "related" | "conflict" | "merged"
    source_uuid: str
    target_uuid: str
    similarity: float
    details: dict[str, Any] | None = None


@dataclass
class CorrelationBatchResult:
    """Aggregate outcome of a batch correlation check."""

    checked: int = 0
    related: int = 0
    conflicts: int = 0
    merged: int = 0
    skipped: int = 0
    results: list[CorrelationResult] | None = None

    def __post_init__(self) -> None:
        if self.results is None:
            self.results = []


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class CorrelationService:
    """Routes high-similarity entity pairs to the correct action.

    Used by:
    - The enrichment pipeline (after new node creation, before finalization)
    - The contradiction batch check (to divert correlation-type matches
      away from the conflict queue)

    Part 2: Judge-gated merge — near-duplicate pairs (>= merge_threshold) are
    proposed for LLM judgment; the merge executes only after deterministic vetoes
    pass and the judge confirms identity.
    """

    def __init__(
        self,
        correlation_repo: CorrelationRepository,
        graphiti_client: Any,
        *,
        related_threshold: float = CORRELATION_RELATED_THRESHOLD,
        conflict_threshold: float = CORRELATION_CONFLICT_THRESHOLD,
        merge_threshold: float = CORRELATION_MERGE_THRESHOLD,
        llm: "LLMAdapter | None" = None,
        merge_coordinator: Any = None,
        on_merge_committed: Any = None,
    ) -> None:
        self._repo = correlation_repo
        self._graphiti = graphiti_client
        self._related_threshold = related_threshold
        self._conflict_threshold = conflict_threshold
        self._merge_threshold = merge_threshold
        self._llm = llm
        self._merge_coordinator = merge_coordinator
        # Optional post-COMMIT hook, injected only when ScalarState (Piece C) is enabled. Called
        # after a merge COMMITs to rebind scalar assertions onto the survivor. None (default) ->
        # no scalar-state coupling, byte-identical to before. Best-effort: it must never fail the
        # merge (the merge already committed; scalar reconciliation is repairable out of band).
        self._on_merge_committed = on_merge_committed

    @property
    def merge_coordinator(self) -> Any:
        """The journaled merge saga (plan Phase 4). Built lazily on the default sidecar.

        Every confirmed merge goes through this, NOT through the raw repository primitive: the
        coordinator commits a complete recovery snapshot as PREPARED before the absorbed node is
        deleted, and verifies the after-state before COMMITTED. The legacy path deleted first and
        wrote a best-effort audit afterwards, so a crash there was unrecoverable.
        """
        if self._merge_coordinator is None:
            from menhir.infrastructure.graph_operations import GraphOperationsJournal
            from menhir.services.merge_coordinator import MergeCoordinator

            self._merge_coordinator = MergeCoordinator(
                graph_adapter=self._repo, journal=GraphOperationsJournal()
            )
        return self._merge_coordinator

    # ------------------------------------------------------------------
    # Pair classification (SSOT-03: the sole owner of routing, vetoes, and
    # judge-gated merge decisions for a single entity pair)
    # ------------------------------------------------------------------

    async def classify_pair(
        self,
        source_uuid: str,
        target_uuid: str,
        similarity: float,
    ) -> tuple[str, dict[str, Any] | None]:
        """Classify one entity pair, execute the routed action, and return the result.

        This is the sole owner of pair classification, deterministic vetoes, and
        judge-gated merge decisions (SSOT-03: previously duplicated -- with a
        missing veto -- in LifecycleService._check_contradictions_batch). Every
        caller that discovers a candidate pair (per-node enrichment check, batch
        contradiction check) must classify it through this method rather than
        reimplementing routing/veto/judge logic itself. Callers retain only
        their own bookkeeping (result counters, conflict-queue flagging,
        telemetry/suppression checks, namespace-scoped search) around the call.

        ``source_uuid`` is the newer/absorbed node; ``target_uuid`` is the
        existing candidate it was matched against (the merge survivor).

        Returns ``(final_action, details)`` where ``final_action`` is one of
        "none" | "related" | "conflict" | "merged".
        """
        action = self._route(similarity)
        if action == "none":
            return "none", None

        if action == "related":
            created = await asyncio.to_thread(
                self._repo.create_related_to_edge,
                source_uuid, target_uuid, similarity=similarity,
            )
            if created:
                logger.info(
                    "Correlation: RELATES_TO %s <-> %s (sim=%.3f)",
                    source_uuid, target_uuid, similarity,
                )
            return "related", {"created": bool(created)}

        if action == "merge_proposed":
            # Part 2: Judge-gated merge — run vetoes + judge, route to merged or conflict
            judged_action = await self._handle_merge_proposal(
                survivor_uuid=target_uuid,
                absorbed_uuid=source_uuid,
                similarity=similarity,
            )
            if judged_action == "merged":
                # Journaled saga (plan Phase 4), NOT the raw repository primitive: the recovery
                # snapshot is committed as PREPARED before the absorbed node is deleted, and the
                # after-state is verified before COMMITTED. An abstention (ineligible pair, fenced
                # by an unresolved operation, snapshot failure) falls through to conflict -- it
                # never silently drops the pair.
                merge_result = await asyncio.to_thread(
                    self.merge_coordinator.merge,
                    survivor_uuid=target_uuid,
                    absorbed_uuid=source_uuid,
                    similarity=similarity,
                )
                if merge_result.get("merged", 0) > 0:
                    logger.info(
                        "Correlation: MERGED %s absorbed into %s (sim=%.3f, judge-confirmed, op=%s)",
                        source_uuid, target_uuid, similarity, merge_result.get("op_id"),
                    )
                    # Post-COMMIT scalar-state reconciliation (Piece C.3), best-effort and only when
                    # wired. The merge is already durable; a failure here is repaired out of band by
                    # the orphan-rebind pass, so it must never turn a committed merge into an error.
                    if self._on_merge_committed is not None:
                        try:
                            await asyncio.to_thread(
                                self._on_merge_committed,
                                absorbed_uuid=source_uuid, survivor_uuid=target_uuid,
                                merge_op_id=str(merge_result.get("op_id") or ""),
                            )
                        except Exception as exc:  # noqa: BLE001
                            logger.warning(
                                "scalar-state merge rebind failed for %s <- %s (op=%s): %s; "
                                "leaving for orphan-rebind repair",
                                target_uuid, source_uuid, merge_result.get("op_id"), exc,
                            )
                    return "merged", merge_result
                logger.info(
                    "Correlation: merge %s <- %s abstained at the saga gate: %s",
                    target_uuid, source_uuid, merge_result.get("reason"),
                )
                return "conflict", {
                    "reason": "merge_abstained",
                    "abstain_reason": merge_result.get("reason"),
                }
            return "conflict", {"reason": "merge_proposal_blocked_by_veto_or_judge"}

        # action == "conflict"
        return "conflict", {"reason": "strong_overlap_pending_review"}

    # ------------------------------------------------------------------
    # Single-node correlation check (used during enrichment)
    # ------------------------------------------------------------------

    async def check_correlation(
        self,
        node_uuid: str,
        query: str,
        *,
        exclude_uuids: set[str] | None = None,
        num_results: int = 10,
        namespace: str | None = None,
    ) -> CorrelationBatchResult:
        """Check a single node for correlation with existing entities.

        Called during enrichment after new nodes are committed to Neo4j.
        The ``query`` is typically the node's content or summary, used
        for the similarity search.

        Args:
            node_uuid: The UUID of the node being checked.
            query: The text to search for similar entities.
            exclude_uuids: Set of UUIDs to exclude from results.
            num_results: Maximum number of similar entities to return.
            namespace: Optional namespace to scope the search (for deterministic consistency).
        """
        if not query.strip():
            return CorrelationBatchResult(checked=1, skipped=1)

        try:
            # Namespace-scoped search (Part 1, deterministic veto: verify namespace-scoped)
            search_kwargs = {"num_results": num_results}
            if namespace:
                from menhir.domain.namespace import namespace_to_group_ids
                search_kwargs["group_ids"] = namespace_to_group_ids(namespace)

            similar = await self._graphiti.search_scored(query, **search_kwargs)
        except Exception:
            logger.warning(
                "Correlation search failed for node=%s", node_uuid, exc_info=True
            )
            return CorrelationBatchResult(checked=1, skipped=1)

        exclude = (exclude_uuids or set()) | {node_uuid}
        result = CorrelationBatchResult(checked=1)

        for other_uuid, other_name, score in similar:
            if other_uuid in exclude:
                continue

            final_action, details = await self.classify_pair(node_uuid, other_uuid, score)
            if final_action == "none":
                continue

            if final_action == "related":
                if details and details.get("created"):
                    result.related += 1
            elif final_action == "merged":
                result.merged += 1
            elif final_action == "conflict":
                result.conflicts += 1

            cr = CorrelationResult(
                action=final_action,
                source_uuid=node_uuid,
                target_uuid=other_uuid,
                similarity=score,
                details=details,
            )
            if result.results is not None:
                result.results.append(cr)

        return result

    # ------------------------------------------------------------------
    # Batch correlation check (NO production caller -- see the docstring)
    # ------------------------------------------------------------------

    async def check_correlation_batch(
        self,
        candidates: list[dict[str, Any]],
        *,
        namespace: str | None = None,
    ) -> CorrelationBatchResult:
        """Check a batch of promotion candidates for correlations.

        Returns results segmented by action so the caller can route
        conflicts to the conflict pipeline and handle merges/edges
        independently.

        **This method has no production caller.** The banner and docstring previously said it was
        "used during consolidation/promotion"; nothing in consolidation or promotion reaches it,
        and the only callers in the corpus are in ``tests/test_correlation_service.py``. That
        claim mattered because anyone wiring this up would reasonably have believed it had already
        been exercised in that role -- with an UNSCOPED search behind it.

        Args:
            candidates: Promotion candidates. Each may carry its own ``namespace``.
            namespace: Scopes the search for every candidate. A candidate's own ``namespace`` key
                is used when this is not supplied, because a batch can span namespaces and pinning
                the whole batch to one would be wrong.

        Scoping matters here specifically: results feed ``classify_pair()`` and merge proposal
        handling, so an unscoped hit is a cross-namespace merge, which is permanent.

        Known residual, shared with ``check_correlation`` and NOT introduced here: when no
        namespace can be resolved the search is global rather than refused. Making that path
        fail closed is a change to both methods and to their existing tests, so it is deliberately
        not made under this finding.
        """
        batch_result = CorrelationBatchResult(checked=len(candidates))

        for node in candidates:
            uuid = str(node["uuid"])
            query = str(node.get("content") or node.get("name") or "")
            if not query.strip():
                batch_result.skipped += 1
                continue

            # Scoped the way `check_correlation` scopes it (see the search there). A batch can
            # span namespaces, so resolve per candidate rather than once for the batch.
            node_namespace = namespace or node.get("namespace")
            search_kwargs: dict[str, Any] = {"num_results": 5}
            if node_namespace:
                from menhir.domain.namespace import namespace_to_group_ids
                search_kwargs["group_ids"] = namespace_to_group_ids(str(node_namespace))

            try:
                similar = await self._graphiti.search_scored(query, **search_kwargs)
            except Exception:
                logger.warning(
                    "Correlation search failed for node=%s", uuid, exc_info=True
                )
                batch_result.skipped += 1
                continue

            for other_uuid, other_name, score in similar:
                if other_uuid == uuid:
                    continue

                final_action, details = await self.classify_pair(uuid, other_uuid, score)
                if final_action == "none":
                    continue

                if final_action == "related":
                    if details and details.get("created"):
                        batch_result.related += 1
                elif final_action == "merged":
                    batch_result.merged += 1
                elif final_action == "conflict":
                    batch_result.conflicts += 1

                cr = CorrelationResult(
                    action=final_action,
                    source_uuid=uuid,
                    target_uuid=other_uuid,
                    similarity=score,
                    details=details,
                )
                if batch_result.results is not None:
                    batch_result.results.append(cr)

                # Only process one match per node to avoid cascading effects
                break

        return batch_result

    # ------------------------------------------------------------------
    # Routing logic
    # ------------------------------------------------------------------

    def _route(self, similarity: float) -> str:
        """Determine the action for a given similarity score.

        Part 2: Judge-gated merge — scores >= merge_threshold route to "merge_proposed",
        which is then handled by vetoes + LLM judge. The judge returns "conflict" on
        non-unanimous or unavailable, implementing fail-safe direction (never merge
        without confirmation).
        """
        if similarity >= self._merge_threshold:
            # Part 2: route to merge proposal (will be judged + vetoed)
            return "merge_proposed"
        if similarity >= self._conflict_threshold:
            return "conflict"
        if similarity >= self._related_threshold:
            return "related"
        return "none"

    async def _handle_merge_proposal(
        self,
        survivor_uuid: str,
        absorbed_uuid: str,
        similarity: float,
    ) -> str:
        """Handle a merge proposal: run vetoes, judge, and decide final action.

        Part 2: Deterministic vetoes first (fast, no model calls). If any veto fires,
        route to conflict. Otherwise, call LLM judge (k=3, unanimous yes only).
        Non-unanimous or judge-unavailable → conflict (fail-safe: never merge without
        confirmation).

        Returns: "merged" if confirmed, "conflict" otherwise.
        """
        # Fetched early (SSOT-08) so the promoted-node veto can run unconditionally,
        # like the other three vetoes -- reused below for the judge prompt so a
        # confirmed merge doesn't re-fetch the same metadata.
        metadata = self._repo.fetch_entity_merge_metadata([survivor_uuid, absorbed_uuid])
        metadata_by_uuid = {m["uuid"]: m for m in metadata}
        meta_a = metadata_by_uuid.get(survivor_uuid, {})
        meta_b = metadata_by_uuid.get(absorbed_uuid, {})

        # Part 1: Run deterministic vetoes (abstain-only; veto downgrades to conflict)
        promoted_veto = str(meta_a.get("scope") or "") == "PROMOTED" or str(meta_b.get("scope") or "") == "PROMOTED"
        ineligible_node_veto = self._repo.check_ineligible_node_veto(survivor_uuid, absorbed_uuid)
        co_mention_veto = self._repo.check_co_mention_veto(survivor_uuid, absorbed_uuid)
        anchor_project_veto = self._repo.check_anchor_project_veto(survivor_uuid, absorbed_uuid)

        if promoted_veto:
            # SSOT-08: a PROMOTED node is operator-curated, verified ground truth --
            # it is never a merge target or source, regardless of similarity or judge
            # availability. This is a hard identity-immutability guarantee, not a
            # confidence signal, so it is checked before (and independent of) the
            # other vetoes and the judge.
            logger.info(
                "Merge proposal %s <-> %s VETOED: PROMOTED node is merge-immune",
                survivor_uuid, absorbed_uuid,
            )
            record_mcp_event(
                kind="background",
                operation="identity_decision",
                payload={
                    "similarity": similarity,
                    "action": "merge_proposed",
                    "survivor_uuid": survivor_uuid,
                    "absorbed_uuid": absorbed_uuid,
                },
                result={
                    "final_action": "conflict",
                    "vetoes_fired": ["promoted_immune"],
                    "judge_available": False,
                },
                success=True,
            )
            return "conflict"

        if ineligible_node_veto:
            logger.info(
                "Merge proposal %s <-> %s VETOED: ineligible node (structural/path-shaped)",
                survivor_uuid, absorbed_uuid,
            )
            # Record identity decision with veto fired
            record_mcp_event(
                kind="background",
                operation="identity_decision",
                payload={
                    "similarity": similarity,
                    "action": "merge_proposed",
                    "survivor_uuid": survivor_uuid,
                    "absorbed_uuid": absorbed_uuid,
                },
                result={
                    "final_action": "conflict",
                    "vetoes_fired": ["ineligible_node"],
                    "judge_available": False,
                },
                success=True,
            )
            return "conflict"

        if co_mention_veto:
            logger.info(
                "Merge proposal %s <-> %s VETOED: co-mention veto (same episode)",
                survivor_uuid, absorbed_uuid,
            )
            # Part 4: Record identity decision with veto fired
            record_mcp_event(
                kind="background",
                operation="identity_decision",
                payload={
                    "similarity": similarity,
                    "action": "merge_proposed",
                    "survivor_uuid": survivor_uuid,
                    "absorbed_uuid": absorbed_uuid,
                },
                result={
                    "final_action": "conflict",
                    "vetoes_fired": ["co_mention"],
                    "judge_available": False,
                },
                success=True,
            )
            return "conflict"

        if anchor_project_veto:
            logger.info(
                "Merge proposal %s <-> %s VETOED: anchor-project veto (different projects)",
                survivor_uuid, absorbed_uuid,
            )
            # Part 4: Record identity decision with veto fired
            record_mcp_event(
                kind="background",
                operation="identity_decision",
                payload={
                    "similarity": similarity,
                    "action": "merge_proposed",
                    "survivor_uuid": survivor_uuid,
                    "absorbed_uuid": absorbed_uuid,
                },
                result={
                    "final_action": "conflict",
                    "vetoes_fired": ["anchor_project"],
                    "judge_available": False,
                },
                success=True,
            )
            return "conflict"

        # Part 2: Run LLM judge (k=3, unanimous yes only)
        if self._llm is None:
            logger.info(
                "Merge proposal %s <-> %s blocked: LLM judge unavailable",
                survivor_uuid, absorbed_uuid,
            )
            # Part 4: Record identity decision with judge unavailable
            record_mcp_event(
                kind="background",
                operation="identity_decision",
                payload={
                    "similarity": similarity,
                    "action": "merge_proposed",
                    "survivor_uuid": survivor_uuid,
                    "absorbed_uuid": absorbed_uuid,
                },
                result={
                    "final_action": "conflict",
                    "vetoes_fired": [],
                    "judge_available": False,
                },
                success=True,
            )
            return "conflict"

        # metadata/meta_a/meta_b already fetched above for the promoted-node veto.
        judge_votes = []
        for judge_id in range(3):  # k=3
            vote = await self._llm.confirm_same_entity(
                name_a=str(meta_a.get("name") or ""),
                content_a=str(meta_a.get("summary") or meta_a.get("content") or ""),
                name_b=str(meta_b.get("name") or ""),
                content_b=str(meta_b.get("summary") or meta_b.get("content") or ""),
            )
            judge_votes.append(vote)
            logger.debug(
                "Judge %d: %s vs %s → %s",
                judge_id, meta_a.get("name"), meta_b.get("name"), vote,
            )

        # Tally votes: merge only on unanimous yes
        yes_votes = sum(1 for v in judge_votes if v is True)
        unanimous = yes_votes == len(judge_votes)
        any_none = any(v is None for v in judge_votes)

        if unanimous and not any_none:
            logger.info(
                "Merge proposal %s <-> %s CONFIRMED by judge (unanimous yes)",
                survivor_uuid, absorbed_uuid,
            )
            # Part 4: Record identity decision with unanimous yes
            record_mcp_event(
                kind="background",
                operation="identity_decision",
                payload={
                    "similarity": similarity,
                    "action": "merge_proposed",
                    "survivor_uuid": survivor_uuid,
                    "absorbed_uuid": absorbed_uuid,
                },
                result={
                    "final_action": "merged",
                    "vetoes_fired": [],
                    "judge_votes": judge_votes,
                    "unanimous": True,
                },
                success=True,
            )
            return "merged"
        else:
            logger.info(
                "Merge proposal %s <-> %s REJECTED by judge (votes: %s)",
                survivor_uuid, absorbed_uuid, judge_votes,
            )
            # Part 4: Record identity decision with split judge or failures
            record_mcp_event(
                kind="background",
                operation="identity_decision",
                payload={
                    "similarity": similarity,
                    "action": "merge_proposed",
                    "survivor_uuid": survivor_uuid,
                    "absorbed_uuid": absorbed_uuid,
                },
                result={
                    "final_action": "conflict",
                    "vetoes_fired": [],
                    "judge_votes": judge_votes,
                    "unanimous": False,
                },
                success=True,
            )
            return "conflict"
