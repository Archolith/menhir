"""Episode intake, processing waits, direct ingest, and memory flag operations."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from time import monotonic, perf_counter
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import httpx

from menhir.domain import IngestResult, IngestStatus, MemorySession
from menhir.domain.models import ProcessingState
from menhir.infrastructure import GraphitiClient, LLMAdapter, MemoryGraphAdapter
from menhir.infrastructure.git_log import current_head
from menhir.infrastructure.paths import repo_root_for_project

if TYPE_CHECKING:
    from menhir.core.bootstrap import (
        UnavailableGraphitiClient,
        UnavailableLLMAdapter,
    )
from menhir.services.scheduler_protocols import LifecycleServiceProtocol
from menhir.infrastructure.observability import (
    LLMUsageEvent,
    reset_llm_usage_callback,
    set_llm_usage_callback,
)
from menhir.infrastructure.scheduler_trace import (
    build_episode_scheduler_task,
)
from menhir.infrastructure.telemetry import (
    record_episode_task_event,
    record_lifecycle_event,
)
from menhir.domain.utils import source_confidence_for
from menhir.infrastructure.circuit_breaker import CircuitOpenError
from menhir.infrastructure.graphiti_patches import clear_extraction_receipt
from menhir.services.enrichment_steps import (
    EnrichmentContext,
    add_episode_with_timeout,
    build_episode_preflight_rejection,
    handle_enrichment_failure,
    run_graphiti_extraction,
    run_preflight_rejection,
    stamp_and_finalize,
    try_reconcile_existing,
)
from menhir.services.ingest_gate import IngestGate

logger = logging.getLogger(__name__)

from menhir.services.ingest_models import (
    _belief_commit_context,
    _parse_occurred_at,
)

class IngestIntakeMixin:
    async def queue_episode_for_enrichment(
        self,
        episode: str,
        session: MemorySession,
        source: str,
        diff: str | None = None,
        flagged: bool = False,
        bootstrap_scope: str | None = None,
        namespace: str | None = None,
        occurred_at: str | None = None,
        turn_evidence_uuid: str | None = None,
    ) -> IngestResult:
        """Durably record a raw episode and queue background enrichment.

        Gate user-tier claims (source='user' or 'manual') using turn evidence grounding before
        persistence. If the claim is ungrounded, downgrade to agent_inference tier.
        """

        from menhir.domain.namespace import stamped_namespace
        from menhir.domain.bootstrap_scope import normalize_bootstrap_scope
        from menhir.domain.truth.admission_gate import evaluate_user_tier_claim

        episode_uuid = str(uuid4())
        episode_name = f"episode-{session.session_id}-{episode_uuid}"
        reference_time = _parse_occurred_at(occurred_at)
        normalized_bootstrap_scope = normalize_bootstrap_scope(bootstrap_scope)
        if normalized_bootstrap_scope is not None and not flagged:
            raise ValueError("bootstrap_scope requires flagged=true")

        try:
            # Gate user-tier claims: fetch turn evidence and evaluate.
            # Rewrite source BEFORE persistence so source_confidence_for() naturally derives correct tier.
            # Wrap in try/except so any fetch_turn_evidence errors fail closed (downgrade to agent_inference).
            effective_source = source
            verdict = None
            if source.strip().lower() in ("user", "manual"):
                try:
                    turn_evidence = None
                    if turn_evidence_uuid:
                        turn_evidence = self.graph_adapter.fetch_turn_evidence(turn_evidence_uuid)
                    verdict = evaluate_user_tier_claim(
                        requested_source=source,
                        turn_evidence=turn_evidence,
                        claimed_text=episode,
                        session_id=session.session_id,
                        namespace=namespace,
                    )
                    effective_source = verdict.effective_source
                except Exception as e:
                    # Fail closed: any gate evaluation error downgrades to agent_inference
                    logger.warning(
                        "Admission gate evaluation failed for session=%s: %s; downgrading to agent_inference",
                        session.session_id, e, exc_info=True,
                    )
                    effective_source = "agent_inference"
                    verdict = evaluate_user_tier_claim(
                        requested_source=source,
                        turn_evidence=None,
                        claimed_text=episode,
                        session_id=session.session_id,
                        namespace=namespace,
                    )

            pending_kwargs: dict[str, object] = {}
            if normalized_bootstrap_scope is not None:
                pending_kwargs["bootstrap_scope"] = normalized_bootstrap_scope
            self.graph_adapter.create_pending_episode(
                episode_uuid=episode_uuid,
                name=episode_name,
                content=episode,
                session_id=session.session_id,
                user_id=session.user_id,
                source=effective_source,
                source_confidence=source_confidence_for(effective_source),
                diff=diff or None,
                user_flagged=flagged,
                namespace=stamped_namespace(namespace),
                reference_time=reference_time,
                **pending_kwargs,
            )

            # Provenance (best-effort): persist the evidence->memory join, so a scalar View founded on
            # this turn can reach the memory built from it (and back).
            #
            # THIS EDGE IS NOT THE TIER DECISION. It records that a memory was written in the context
            # of a captured turn, which is true of EVERY source; the gate above decides whether a
            # client may CLAIM user authority, which is a different question. The two were welded
            # together only because both need the same uuid, and the cost was severe: this block used
            # to live inside the `source in ("user", "manual")` branch, so agent-sourced memories --
            # claude-code, codex, opencode, i.e. all of production -- never drew one. G14's scalar
            # loader hands out `:TurnEvidence.turn_id`s whenever any Turn evidence exists, and
            # `fetch_linked_entities_for_episode` resolves those back to an episode THROUGH THIS EDGE.
            # Absent it, the resolution finds nothing and non-self scalar binding is dead: no entity
            # candidates, so every non-self subject abstains. The benchmark cannot see this because it
            # ingests as source='user'.
            #
            # Two sources for the id, and the gate keeps its veto over the one it owns:
            #  - gated (user/manual): only on a GRANT. A denial means the cited evidence did NOT
            #    support the claim, so drawing the edge would assert the foundation the gate just
            #    refused, and `scalar_view_has_user_foundation` would read it as a real user admission.
            #  - ungated (any other source): no user-tier claim was made, so there is nothing to
            #    refuse and nothing to forge -- the tier is already agent, and this edge cannot raise
            #    it. The caller-supplied id is taken at face value; `link_episode_admission` is
            #    MATCH-only on both endpoints, so an id naming nothing draws nothing.
            #
            # Never block the ingest on it -- the episode is already durable and a missing provenance
            # edge must not fail the write.
            evidence_projection_uuid: str | None = None
            if verdict is not None:
                admitted_on_uuid = verdict.turn_evidence_uuid if verdict.granted else None
            else:
                admitted_on_uuid = (turn_evidence_uuid or "").strip() or None
            if admitted_on_uuid:
                try:
                    self.graph_adapter.link_episode_admission(
                        episode_uuid=episode_uuid,
                        turn_evidence_uuid=admitted_on_uuid,
                        # Scoped, and it was NOT before. `link_episode_admission` filters its two
                        # MATCHes only when a namespace is supplied -- None deliberately means
                        # "do not filter" -- so omitting it here let a non-user source draw
                        # `episode in A -[:ADMITTED_ON]-> TurnEvidence in B`, from a caller-supplied
                        # id taken at face value. CF-225 scoped the QUERY; this is the call site
                        # that was still passing None into it.
                        #
                        # `stamped_namespace` matches the projection call below, which already
                        # passed it: one normalisation, so the edge and the projection cannot
                        # disagree about which silo this write belongs to.
                        namespace=stamped_namespace(namespace),
                    )
                except Exception:
                    logger.debug(
                        "Failed to link episode=%s to turn evidence=%s; provenance edge missing "
                        "(non-fatal)",
                        episode_uuid, admitted_on_uuid, exc_info=True,
                    )

                # Evidence projection (best-effort): also enrich the TURN's verbatim text, so entities
                # exist in the user's own vocabulary rather than only in this memory's paraphrase of
                # it. Without it a scalar assertion extracted from "I have 25 postcards" has to bind
                # against entities extracted from whatever the agent wrote instead, and the two
                # vocabularies need not agree. Idempotent on the turn, and fail-closed on declarant --
                # see EpisodeLifecycleRepository.create_evidence_projection.
                #
                # Strictly additive, like the edge above: the memory is already durable, so a failed
                # projection must not fail the write.
                try:
                    evidence_projection_uuid = self.graph_adapter.create_evidence_projection(
                        turn_evidence_uuid=admitted_on_uuid,
                        projection_uuid=str(uuid4()),
                        name=f"evidence-projection-{admitted_on_uuid}",
                        session_id=session.session_id,
                        user_id=session.user_id,
                        namespace=stamped_namespace(namespace),
                    )
                except Exception:
                    logger.debug(
                        "Failed to project turn evidence=%s; entities will come only from the "
                        "memory text (non-fatal)",
                        admitted_on_uuid, exc_info=True,
                    )

            # Audit trail (best-effort): only DOWNGRADES/DENIALS carry signal and are recorded.
            # A routine grant (granted=True, effective_source==requested_source) previously wrote one
            # "Admission granted: <client> claimed user" node PER user turn — high-volume spam; those
            # now log at debug only. And the kept audits go to the dedicated telemetry silo
            # "agent-status" (same namespace as verifier-sync config registers), NOT the user's
            # namespace — admission telemetry must never pollute user memory or compete for
            # scalar-perception extraction budget. Never block on the audit write.
            if verdict is not None and not verdict.granted:
                try:
                    self.graph_adapter.record_admission_audit(
                        subject=session.user_id or session.session_id,
                        namespace="agent-status",  # telemetry silo, not the user namespace
                        requested_source=source,
                        effective_source=verdict.effective_source,
                        granted=verdict.granted,
                        turn_evidence_uuid=verdict.turn_evidence_uuid,
                        reason=verdict.reason,
                    )
                except Exception:
                    logger.debug(
                        "Failed to record admission audit for session=%s; audit trail incomplete (non-fatal)",
                        session.session_id, exc_info=True,
                    )
            elif verdict is not None:
                # Routine grant (admitted exactly as requested): intentionally no audit node.
                logger.debug(
                    "Admission granted as requested (source=%s) for session=%s; routine grant, audit suppressed",
                    source, session.session_id,
                )

            if self._enrichment_enabled:
                await self._ensure_enrichment_worker()
                await self._enqueue_pending_episode(episode_uuid)
                # The projection is a real PENDING episode and enriches through the same worker;
                # without this it would sit queued forever and produce none of the entities it exists
                # to produce. Enqueued AFTER the memory so the curated write is never delayed by it.
                if evidence_projection_uuid:
                    await self._enqueue_pending_episode(evidence_projection_uuid)
        except Exception:  # catch-all: episode queue must not crash the caller
            logger.exception(
                "Episode queue failed for session_id=%s source=%s",
                session.session_id,
                source,
            )
            return IngestResult(
                episode_id="",
                status=IngestStatus.FAILED,
                nodes_touched=0,
                edges_touched=0,
            )

        return IngestResult(
            episode_id=episode_uuid,
            status=IngestStatus.QUEUED,
            nodes_touched=1,
            edges_touched=0,
        )

    async def wait_for_episode_processing(
        self,
        episode_uuid: str,
        *,
        timeout_s: float,
        poll_interval_s: float = 0.1,
    ) -> dict[str, object] | None:
        """Wait briefly for one episode anchor to leave PENDING/ENRICHING."""

        deadline = perf_counter() + max(0.0, timeout_s)
        while perf_counter() < deadline:
            row = await asyncio.to_thread(self.graph_adapter.fetch_episode_processing, episode_uuid)
            if row is None:
                return None
            # ProcessingState is a (str, Enum) mixin. str(ProcessingState.READY) returns
            # the qualified repr "ProcessingState.READY", not the value "READY" -- so
            # wrapping in str() here (as this used to) made the membership check below
            # never match, and this method would always poll for the full timeout_s
            # regardless of the row's actual state. Compare the raw value directly:
            # this matches whether the row holds the enum member itself or a plain
            # string "READY"/"FAILED" (e.g. round-tripped through a real Neo4j driver),
            # since str-mixin enums compare equal to their string value either way.
            if row.get("processing_state") in (ProcessingState.READY, ProcessingState.FAILED):
                return row
            await asyncio.sleep(poll_interval_s)
        return await asyncio.to_thread(self.graph_adapter.fetch_episode_processing, episode_uuid)


    async def ingest_episode(
        self,
        episode: str,
        session: MemorySession,
        source: str,
        namespace: str | None = None,
    ) -> IngestResult:
        """Ingest an episode through Graphiti using a session-aware async interface.

        PART 3: Unified queue-backed path. This method now delegates to the background
        enrichment pipeline for consistency with the queued path. Synchronous callers
        keep their semantics — READY within the wait returns INGESTED with real counts;
        timeout returns QUEUED (honest); FAILED returns FAILED.

        The old inline Graphiti extraction body (which had no revision records, no
        correlation, no structural anchoring, no fact repair, no rehydration) is deleted
        in favor of the unified queue path that includes all these steps.
        """

        if not self._enrichment_enabled:
            logger.warning(
                "Direct episode ingest requested while enrichment is disabled: %s",
                self._enrichment_disabled_reason or "enrichment_disabled",
            )
            return IngestResult(
                episode_id="",
                status=IngestStatus.FAILED,
                nodes_touched=0,
                edges_touched=0,
            )

        try:
            # Queue the episode for background enrichment (durably records it first)
            queue_result = await self.queue_episode_for_enrichment(
                episode=episode,
                session=session,
                source=source,
                namespace=namespace,
            )
            if queue_result.status != IngestStatus.QUEUED:
                # Queueing failed (likely preflight rejection)
                return queue_result

            episode_uuid = queue_result.episode_id

            # Wait for processing with bounded timeout (60s)
            processing_row = await self.wait_for_episode_processing(
                episode_uuid=episode_uuid,
                timeout_s=60.0,
            )

            if processing_row is None:
                # Episode not found in graph
                return IngestResult(
                    episode_id=episode_uuid,
                    status=IngestStatus.FAILED,
                    nodes_touched=0,
                    edges_touched=0,
                )

            # Same fix as wait_for_episode_processing above: ProcessingState is a
            # (str, Enum) mixin, and str(ProcessingState.READY) returns the qualified
            # repr "ProcessingState.READY", not the value "READY" -- so comparing that
            # wrapped string against the enum members below could never match, and
            # ingest_episode always fell through to reporting QUEUED even when the
            # episode had actually completed (READY) or failed (FAILED) within the
            # timeout window. Compare the raw value directly.
            processing_state = processing_row.get("processing_state")

            if processing_state == ProcessingState.READY:
                # Enrichment completed within timeout
                return IngestResult(
                    episode_id=episode_uuid,
                    status=IngestStatus.INGESTED,
                    nodes_touched=int(processing_row.get("enriched_nodes_touched", 0)),
                    edges_touched=int(processing_row.get("enriched_edges_touched", 0)),
                )
            elif processing_state == ProcessingState.FAILED:
                # Enrichment failed
                return IngestResult(
                    episode_id=episode_uuid,
                    status=IngestStatus.FAILED,
                    nodes_touched=0,
                    edges_touched=0,
                )
            else:
                # Still PENDING/ENRICHING after timeout — return QUEUED (honest)
                return IngestResult(
                    episode_id=episode_uuid,
                    status=IngestStatus.QUEUED,
                    nodes_touched=0,
                    edges_touched=0,
                )

        except Exception:  # catch-all: episode ingest must not crash the caller
            logger.exception(
                "Episode ingest failed for session_id=%s source=%s",
                session.session_id,
                source,
            )
            return IngestResult(
                episode_id="",
                status=IngestStatus.FAILED,
                nodes_touched=0,
                edges_touched=0,
            )

    def flag_memory(self, node_id: str) -> bool:
        """Record the explicit v1 user retention override for a memory node."""

        return self.graph_adapter.flag_memory(node_id)

    def unflag_memory(self, node_id: str) -> bool:
        """Remove the explicit user retention override from a memory node."""

        return self.graph_adapter.unflag_memory(node_id)

    def delete_memory(self, node_id: str) -> bool:
        """Delete a memory node and all its relationships from the graph."""

        return self.graph_adapter.delete_memory(node_id)
