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

Thresholds are based on the audit of 11,412 entities. The conflict threshold is
defined once, as CORRELATION_CONFLICT_THRESHOLD in correlation_service_types;
lifecycle_models.SIMILARITY_CONFLICT_THRESHOLD is the same constant.

The score routed on is an ordinal RRF rank score, not the cosine these bands
describe (see ``classify_pair``). A real name cosine below
CORRELATION_SIMILARITY_FLOOR drops conflict and merge candidates before review.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, TYPE_CHECKING

from menhir.infrastructure.correlation_queries import CorrelationRepository
from menhir.infrastructure.telemetry import record_mcp_event

from menhir.services import auto_mutation_policy as amp

# Facade re-exports: thresholds and result types live in correlation_service_types;
# the search-driven check methods live on CorrelationChecksMixin.
from menhir.services.correlation_service_checks import CorrelationChecksMixin
from menhir.services.correlation_service_signals import attributes_overlap, pair_cosine
from menhir.services.correlation_service_types import (
    CORRELATION_CONFLICT_THRESHOLD,
    CORRELATION_MERGE_THRESHOLD,
    CORRELATION_RELATED_THRESHOLD,
    CORRELATION_SIMILARITY_FLOOR,
    CorrelationBatchResult,
    CorrelationResult,
)

__all__ = [
    "CORRELATION_CONFLICT_THRESHOLD",
    "CORRELATION_MERGE_THRESHOLD",
    "CORRELATION_RELATED_THRESHOLD",
    "CORRELATION_SIMILARITY_FLOOR",
    "CorrelationBatchResult",
    "CorrelationResult",
    "CorrelationService",
]

if TYPE_CHECKING:
    from menhir.core.bootstrap import UnavailableGraphitiClient
    from menhir.infrastructure.graphiti_client import GraphitiClient
    from menhir.infrastructure.llm import LLMAdapter
    from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
    from menhir.services.merge_coordinator import MergeCoordinator

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class CorrelationService(CorrelationChecksMixin):
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
        correlation_repo: "CorrelationRepository | MemoryGraphAdapter",
        graphiti_client: "GraphitiClient | UnavailableGraphitiClient",
        *,
        related_threshold: float = CORRELATION_RELATED_THRESHOLD,
        conflict_threshold: float = CORRELATION_CONFLICT_THRESHOLD,
        merge_threshold: float = CORRELATION_MERGE_THRESHOLD,
        llm: "LLMAdapter | None" = None,
        merge_coordinator: "MergeCoordinator | None" = None,
        similarity_floor: float = CORRELATION_SIMILARITY_FLOOR,
    ) -> None:
        self._repo = correlation_repo
        self._graphiti = graphiti_client
        self._related_threshold = related_threshold
        self._conflict_threshold = conflict_threshold
        self._merge_threshold = merge_threshold
        self._llm = llm
        self._merge_coordinator = merge_coordinator
        # No post-COMMIT scalar hook (#872). Scalar assertions bound to an absorbed node are moved
        # onto the survivor by the scalar lane's repair pass: run_scalar_consolidation ->
        # ScalarStateService.repair_incomplete_reconciliations replays every COMMITTED ENTITY_MERGE
        # in the journal that has no ScalarReconcile receipt (handle_merge, per namespace), then
        # repair_orphaned_assertions. That pass is the only mechanism, and the only one that
        # resolves the op's namespace.
        self._similarity_floor = similarity_floor

    @property
    def merge_coordinator(self) -> "MergeCoordinator":
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
        rrf_score: float,
    ) -> tuple[str, dict[str, Any] | None]:
        """Classify one entity pair, execute the routed action, and return the result.

        ``rrf_score`` is graphiti's RRF rank-fusion score from ``search_scored`` (range ~[0, 2],
        rank_const=1: 1.0 for a lane's top hit, 0.5 for its second). It is ORDINAL, NOT a cosine
        similarity, although the 0.70 / 0.85 / 0.95 thresholds it is compared with are written as
        cosine values (RCA ``rca-conflict-detection-rrf-scale-mismatch-2026-08-09``). The name says
        so on purpose. Routing stays on this score because cosine cannot tell a contradiction from
        a duplicate; a real name cosine only drops conflict and merge candidates below
        ``similarity_floor``, so the vetoes, judge and review still decide everything above it.

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
        "none" | "related" | "conflict" | "merged". ``none`` with details means the pair was
        dropped by the floor or the veto; a caller that stops at a match must stop there too, or a
        drop reaches hits it would not have classified.

        ``none`` with ``{"reason": "AUTO_MERGE_OFF" | "AUTO_MERGE_PROPOSED"}`` means the vetoes and
        judge confirmed a duplicate but MENHIR_AUTO_MERGE_MODE is not "on", so nothing was merged
        (propose records a pending proposal). It is not a conflict: a confirmed duplicate is not a
        contradiction, and a judge "no" still returns ``conflict`` in every mode.
        """
        similarity = rrf_score  # downstream names/telemetry keys stay `similarity` (unchanged)
        action = self._route(similarity)
        if action == "none":
            return "none", None

        if action in ("conflict", "merge_proposed") and await self._below_similarity_floor(
            source_uuid, target_uuid, rrf_score, action,
        ):
            return "none", {"reason": "below_similarity_floor"}

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
                    return "merged", merge_result
                if merge_result.get("reason") in (
                    amp.AUTO_MERGE_OFF, amp.AUTO_MERGE_PROPOSED
                ):
                    return "none", {"reason": merge_result["reason"]}
                logger.info(
                    "Correlation: merge %s <- %s abstained at the saga gate: %s",
                    target_uuid, source_uuid, merge_result.get("reason"),
                )
                return "conflict", {
                    "reason": "merge_abstained",
                    "abstain_reason": merge_result.get("reason"),
                }
            # A shared-episode pair blocked here by the co-mention veto used to fall through to the
            # conflict queue -- the veto proved the pair distinct and it was queued anyway.
            if await self._shared_episode_veto(source_uuid, target_uuid, stage="merge_blocked"):
                return "none", {"reason": "shared_episode_veto"}
            return "conflict", {"reason": "merge_proposal_blocked_by_veto_or_judge"}

        # action == "conflict"
        if await self._shared_episode_veto(source_uuid, target_uuid, stage="conflict"):
            return "none", {"reason": "shared_episode_veto"}
        return "conflict", {"reason": "strong_overlap_pending_review"}

    # ------------------------------------------------------------------
    # Cheap checks in front of review: similarity floor, shared-episode veto
    # ------------------------------------------------------------------

    async def _below_similarity_floor(
        self, source_uuid: str, target_uuid: str, rrf_score: float, route: str,
    ) -> bool:
        """True when a pair bound for review or merge is too dissimilar to be worth reviewing.

        A drop needs a defined name cosine below the floor and both members read as not PROMOTED
        (SSOT-08). A missing vector, an unreadable member and any error return False, which keeps
        today's route: no signal is not low similarity. This can only turn a route into ``none``.
        A drop writes nothing to the graph, so a rescan of the node records it again.
        """
        try:
            vec_a, vec_b = await asyncio.to_thread(
                self._repo.fetch_pair_name_embeddings, source_uuid, target_uuid,
            )
            cosine = pair_cosine(vec_a, vec_b)
            if cosine is None or not cosine < self._similarity_floor:
                return False
            metadata = await asyncio.to_thread(
                self._repo.fetch_entity_merge_metadata, [source_uuid, target_uuid],
            )
            scopes = {str(m.get("uuid")): str(m.get("scope") or "") for m in metadata or []}
            if {source_uuid, target_uuid} - scopes.keys() or "PROMOTED" in scopes.values():
                return False
            record_mcp_event(
                kind="background",
                operation="correlation_similarity_floor",
                payload={
                    "source_uuid": source_uuid,
                    "target_uuid": target_uuid,
                    "rrf_score": rrf_score,
                    "cosine": cosine,
                    "action": route,
                },
                node_uuid=source_uuid,
                success=True,
            )
        except Exception:  # noqa: BLE001 -- fail open: an unreadable pair routes as before
            logger.debug(
                "Similarity floor check failed for %s <-> %s; keeping route",
                source_uuid, target_uuid, exc_info=True,
            )
            return False
        logger.info(
            "Correlation %s <-> %s dropped below similarity floor (cosine=%.3f rrf=%.3f route=%s)",
            source_uuid, target_uuid, cosine, rrf_score, route,
        )
        return True

    async def _shared_episode_veto(self, source_uuid: str, target_uuid: str, *, stage: str) -> bool:
        """True when a conflict-bound pair must be dropped before it is queued.

        Safety property: a pair is dropped only if BOTH members are MENTIONed by one episode (a
        single source asserted both, which is affirmative evidence of compatibility) AND neither is PROMOTED AND their
        names share no attribute word (so it is not a same-turn correction such as "retry limit
        was 5, actually 7"). Every other case, and every error, returns False -- i.e. today's
        behaviour. Reached only from the two ``conflict`` outcomes of ``classify_pair``; the merge
        path (vetoes, judge, saga) is not touched.

        Known limit: the overlap check is word-based, so a same-episode correction whose names
        share no word ("Lisbon residence" / "Porto home") is dropped. Each decision is recorded.
        """
        try:
            shared = await asyncio.to_thread(
                self._repo.check_co_mention_veto, source_uuid, target_uuid,
            )
            if not shared:
                return False
            metadata = await asyncio.to_thread(
                self._repo.fetch_entity_merge_metadata, [source_uuid, target_uuid],
            )
            names = {str(m.get("uuid")): m.get("name") for m in metadata or []}
            if any(str(m.get("scope") or "") == "PROMOTED" for m in metadata or []):
                return False  # operator-curated ground truth is never silently dropped (SSOT-08)
            overlap = attributes_overlap(names.get(source_uuid), names.get(target_uuid))
        except Exception:  # noqa: BLE001 -- fail open: an unreadable pair is queued as before
            logger.debug(
                "Shared-episode veto check failed for %s <-> %s; not vetoing",
                source_uuid, target_uuid, exc_info=True,
            )
            return False
        record_mcp_event(
            kind="background",
            operation="conflict_shared_episode_veto",
            payload={
                "source_uuid": source_uuid,
                "target_uuid": target_uuid,
                "phase": stage,
                "vetoed": not overlap,
                "attribute_overlap": overlap,
            },
            node_uuid=source_uuid,
            success=True,
        )
        if overlap:
            return False
        logger.info(
            "Conflict candidate %s <-> %s VETOED: shared episode, non-overlapping attributes (%s)",
            source_uuid, target_uuid, stage,
        )
        return True

    # ------------------------------------------------------------------
    # Routing logic
    # ------------------------------------------------------------------

    def _route(self, similarity: float) -> str:
        """Determine the action for a given score on the SAME scale as the thresholds.

        Production passes an RRF rank-fusion score (see ``classify_pair``), which is NOT on the
        cosine scale these thresholds are written in.

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

    def _read_merge_veto_inputs(
        self, survivor_uuid: str, absorbed_uuid: str,
    ) -> tuple[list[dict[str, Any]], bool, bool, bool]:
        """The four blocking repository reads behind a merge proposal. Run via asyncio.to_thread.

        Returns ``(metadata, ineligible_node_veto, co_mention_veto, anchor_project_veto)``.
        """
        return (
            self._repo.fetch_entity_merge_metadata([survivor_uuid, absorbed_uuid]),
            self._repo.check_ineligible_node_veto(survivor_uuid, absorbed_uuid),
            self._repo.check_co_mention_veto(survivor_uuid, absorbed_uuid),
            self._repo.check_anchor_project_veto(survivor_uuid, absorbed_uuid),
        )

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
        # Metadata is fetched early (SSOT-08) so the promoted-node veto can run unconditionally,
        # like the other three vetoes -- reused below for the judge prompt so a
        # confirmed merge doesn't re-fetch the same metadata. All four reads run off the
        # event loop in one worker-thread hop (#872).
        metadata, ineligible_node_veto, co_mention_veto, anchor_project_veto = (
            await asyncio.to_thread(self._read_merge_veto_inputs, survivor_uuid, absorbed_uuid)
        )
        metadata_by_uuid = {m["uuid"]: m for m in metadata}
        meta_a = metadata_by_uuid.get(survivor_uuid, {})
        meta_b = metadata_by_uuid.get(absorbed_uuid, {})

        # Part 1: Run deterministic vetoes (abstain-only; veto downgrades to conflict)
        promoted_veto = str(meta_a.get("scope") or "") == "PROMOTED" or str(meta_b.get("scope") or "") == "PROMOTED"

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
            # CF-96: uuids, not names. This is the finding's demonstrated leak -- two entity
            # names rendered through unquoted %s, which `redact_log_line` cannot see because it
            # only masks QUOTED spans. Quoting them here would not fix it either: a single-word
            # name is under the 12-char free-text floor and would pass the heuristic anyway.
            # The uuids are already in scope, are structural by `privacy.STRUCTURAL_FIELDS`, and
            # identify the pair for anyone debugging a judge vote. Not emitting the content beats
            # emitting it and hoping a regex catches it.
            logger.debug(
                "Judge %d: %s vs %s → %s",
                judge_id, survivor_uuid, absorbed_uuid, vote,
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
                    # Telemetry only; MergeCoordinator owns the gate.
                    "final_action": (
                        "merged" if amp.auto_merge_mode() == "on" else "merge_declined"
                    ),
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
