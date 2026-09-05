"""Extracted enrichment pipeline step functions.

Each function implements one stage of the background enrichment pipeline.
They accept an ``EnrichmentContext`` dataclass (or explicit parameters for
dual-path helpers) so they are independently testable outside of IngestService.
"""

from __future__ import annotations

import asyncio
import logging
import re
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from time import perf_counter
from typing import Any, Callable

from menhir.services.scheduler_protocols import LifecycleServiceProtocol

from menhir.domain.models import FreshnessState, ProcessingState
from menhir.domain.self_identity import (
    SelfEvidenceKind,
    SelfIdentityContext,
    SelfSubjectEndpointEnvelope,
    normalize_logical_namespace,
    self_context_for_pending_episode,
    self_subject_endpoint_for_claim,
)
from menhir.domain.self_authority import SELF_ASSERTION_POLICY_VERSION
from menhir.infrastructure.self_binding import (
    InvalidSelfSubjectDeclarationError,
    SelfBindMode,
    resolve_bind_mode,
)
from menhir.infrastructure.self_authority import FileSelfAssertionAuthorizer
from menhir.domain.utils import source_confidence_for
from menhir.infrastructure import GraphitiClient, MemoryGraphAdapter
from menhir.infrastructure.evidence_publication_intents import (
    EvidencePublicationIntentRepository,
    PublicationDispatchSuppressed,
)
from menhir.infrastructure.scheduler_trace import (
    build_episode_parent_metadata,
    emit_scheduler_task_event,
)
from menhir.infrastructure.telemetry import (
    record_failure_event,
    record_lifecycle_event,
    record_mcp_event,
    record_memory_revision,
)
from menhir.infrastructure.graphiti_helpers import SYNTHETIC_FACT_PREFIX, strip_synthetic_prefix
from menhir.infrastructure.graphiti_patches import (
    begin_extraction_receipt,
    is_policy_empty_extraction,
    clear_extraction_receipt,
    get_extraction_receipt,
)
from menhir.services.enrichment_failures import (
    classify_enrichment_failure,
    is_graphiti_output_parse_error,
)
from menhir.services.ingest_gate import IngestGate
from menhir.services.shadow_context_composition import (
    build_shadow_trace,
    run_shadow_composition_with_timeout,
    shadow_trace_to_details,
    snapshot_candidate_facts,
)

logger = logging.getLogger(__name__)


class CombinedExtractionCollapsedError(RuntimeError):
    """Raised when combined extraction produced a non-empty payload but persisted nothing.

    Distinguishes a genuine empty extraction ("ok thanks" — no memorable content) from
    a collapse where the LLM DID extract entities/edges but Graphiti's resolution dropped
    them all (dangling endpoints, orphan pruning, malformed rows). The message carries the
    substring ``combined_extraction_collapsed`` so ``classify_enrichment_failure`` routes
    it to the retryable path instead of masking it as an empty-extraction success.
    """


# ---------------------------------------------------------------------------
# Context object — carries per-episode state + adapters into step functions
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EnrichmentContext:
    """Immutable bag of per-episode state and adapters passed to pipeline steps."""

    # Per-episode state
    episode_uuid: str
    claimed: dict[str, object]
    started: float
    processing_attempts: int
    # Identity
    worker_id: str
    # Adapters
    graph_adapter: MemoryGraphAdapter
    graphiti_client: GraphitiClient
    lifecycle_service: LifecycleServiceProtocol | None
    llm: Any | None
    # Synchronization — bounded concurrency + per-namespace serialization
    ingest_gate: IngestGate
    # Config
    processing_steps_total: int
    settings_record_revisions: bool
    ready_warning_ms: int
    graphiti_add_episode_timeout_s: float
    graphiti_episode_max_estimated_tokens: int
    # Callback
    get_queue_depth: Callable[[], int]
    # Stage 1 shadow-mode context composition (observe-only; see shadow_context_composition.py
    # and .agent/plans/menhir-context-composition-production-integration.md). Off by default —
    # every EnrichmentContext construction site that predates this field gets the safe default.
    shadow_context_composition: bool = False
    shadow_composition_timeout_s: float = 30.0
    # Registers a fire-and-forget background task so IngestService can track/drain it at
    # shutdown. None (the default) means "don't track" — used by tests that construct
    # EnrichmentContext directly without a real IngestService behind it.
    register_background_task: Callable[[asyncio.Task], None] | None = None
    # Optional, activation-gated publication protocol.  No runtime/bootstrap path constructs this
    # repository yet because the managed tombstone HMAC key ring and created-only Graphiti artifact
    # manifest do not exist.  Tests and a future explicit activation hook can inject it without
    # changing the extraction API; absence preserves the currently deployed path.
    evidence_publication_intents: EvidencePublicationIntentRepository | None = None
    #: Canonical-self binding rollout: "off" (default), "observe" or "enforce". Defaulted so every
    #: construction site predating this field keeps pre-change behavior.
    canonical_self_binding_mode: str = "off"
    #: Read-only owner-confirmation configuration. All three are required in enforce mode; any
    #: absent or mismatched value leaves semantic self assertions as proposals only.
    canonical_self_confirmation_public_key_path: str = ""
    canonical_self_confirmation_public_key_sha256: str = ""
    canonical_self_confirmation_directory: str = ""


# ---------------------------------------------------------------------------
# Pure helpers (no ctx needed)
# ---------------------------------------------------------------------------

def failure_details_from_exception(exc: Exception) -> dict[str, object]:
    """Extract optional structured diagnostics carried on enrichment exceptions."""

    details = getattr(exc, "menhir_failure_details", None)
    if isinstance(details, dict):
        return dict(details)
    return {}


def propagate_user_flag(
    graph_adapter: Any,
    node_uuids: list[str],
    *,
    episode_uuid: str,
) -> None:
    """Flag each extracted entity node for retention, skipping structural nodes.

    Single source of truth for propagating an episode's ``user_flagged`` onto the
    entity nodes extracted from it. Entity resolution can dedupe an extracted node
    onto an existing structural graph node (project/directory/file/document), which
    ``flag_memory`` refuses by design with ``ValueError``; those are skipped rather
    than raised so a single dedup-onto-structural node cannot abort the episode's
    enrichment or reconcile. Every enrichment/reconcile path must call this instead
    of looping over ``flag_memory`` directly (the guard previously lived in three
    copies and drifted out of sync).

    Propagates retention only. The episode's ``bootstrap_scope`` is deliberately NOT
    forwarded: ``user_flagged`` is the retention override, ``bootstrap_scope`` is the
    separate startup-selection bit (domain/bootstrap_scope.py). Forwarding it put every
    extracted entity -- including shared hubs like "PostgreSQL 16" -- into the bootstrap
    read, which requires both. An entity still gets a scope when flagged directly via
    ``flag_memory(uuid, bootstrap_scope=...)``, and that existing scope survives here
    because scope-less ``flag_memory`` leaves the property untouched.
    """

    for node_uuid in node_uuids:
        try:
            graph_adapter.flag_memory(node_uuid)
        except ValueError:
            logger.debug(
                "Skipping flag on structural node uuid=%s during flag propagation "
                "for episode_id=%s",
                node_uuid,
                episode_uuid,
            )


# Maximum diff size (in characters) appended to episode bodies.
# Larger diffs are truncated to avoid exceeding Neo4j string property limits
# and inflating LLM token usage during enrichment.
MAX_DIFF_CHARS = 50_000


def compose_episode_body(claimed: dict[str, object]) -> str:
    """Build the episode body sent to Graphiti, appending any attached diff."""

    content = str(claimed.get("content") or "")
    diff = claimed.get("diff")
    if not diff:
        return content
    diff_text = str(diff).strip()
    if not diff_text:
        return content
    if len(diff_text) > MAX_DIFF_CHARS:
        diff_text = diff_text[:MAX_DIFF_CHARS] + "\n... [diff truncated]"
    return f"{content}\n\n--- git diff ---\n{diff_text}"


def coerce_reference_time(value: object | None) -> datetime:
    """Convert stored graph timestamps to a timezone-aware Python datetime.

    `None` is the EXPECTED default: a live turn has no `occurred_at` and "now" is the correct world
    time for it. A NON-None value that is not a datetime is different -- a caller supplied a time and
    it is being discarded, so the episode will be stamped with ingestion time instead of when it
    actually happened. That must be loud.

    Silence here cost a corpus. On 2026-07-02 archolith-bench recorded that menhir "does NOT backdate
    a fresh benchmark ingest" and shipped a backfill script rather than a fix; the scalar-ku corpus
    built on 2026-07-22 landed with 1707 of 2862 episodes (62%) carrying ingestion time as `valid_at`.
    Supersession orders by `valid_at`, so "which value is current" was decided by ingest order for
    most of that corpus -- and nothing anywhere logged above DEBUG while it happened.
    """

    if hasattr(value, "to_native"):
        value = value.to_native()
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    if value is not None:
        logger.warning(
            "reference_time %r (%s) is not a datetime; stamping this episode with ingestion time. "
            "Backdated history will supersede in the WRONG order.",
            value, type(value).__name__,
        )
    return datetime.now(timezone.utc)


def estimate_episode_tokens(episode_body: str) -> int:
    """Estimate prompt tokens conservatively from raw text length."""

    rendered = str(episode_body or "")
    if not rendered:
        return 0
    return max(1, (len(rendered) + 3) // 4)


def build_episode_preflight_rejection(
    episode_body: str,
    max_estimated_tokens: int,
) -> dict[str, int | str] | None:
    """Return a synthetic terminal error when the raw episode is obviously too large.

    Takes explicit params (not ctx) because the legacy ``ingest_episode()``
    path also calls this function.
    """

    limit = max(0, int(max_estimated_tokens))
    if limit == 0:
        return None

    rendered = str(episode_body or "")
    char_count = len(rendered)
    estimated_tokens = estimate_episode_tokens(rendered)
    if estimated_tokens <= limit:
        return None

    return {
        "code": "episode_preflight_too_large",
        "error": (
            "episode_preflight_too_large "
            f"estimated_tokens={estimated_tokens} limit={limit} chars={char_count}"
        ),
        "estimated_tokens": estimated_tokens,
        "limit": limit,
        "char_count": char_count,
    }


def still_owns_episode(
    graph_adapter: MemoryGraphAdapter,
    episode_uuid: str,
    worker_id: str,
) -> bool:
    """Check whether this worker still holds the enrichment lease."""

    row = graph_adapter.fetch_episode_processing(episode_uuid)
    if row is None:
        return False
    return (
        row.get("processing_state") == ProcessingState.ENRICHING
        and str(row.get("processing_owner") or "") == worker_id
    )


# ---------------------------------------------------------------------------
# Pipeline step 1 — try to reconcile an already-completed Graphiti result
# ---------------------------------------------------------------------------

async def try_reconcile_existing(ctx: EnrichmentContext) -> bool:
    """Check for an existing Graphiti completion and mark ready. Returns True if handled."""

    existing_completion = ctx.graph_adapter.find_completed_episode_artifact(
        anchor_uuid=ctx.episode_uuid,
        anchor_name=str(ctx.claimed.get("name") or ctx.episode_uuid),
    )
    if existing_completion is not None:
        resolved_episode_uuid = str(existing_completion.get("resolved_episode_uuid") or "")
        entity_uuids = [str(uuid) for uuid in (existing_completion.get("entity_uuids") or []) if str(uuid)]
        edge_uuids = [str(uuid) for uuid in (existing_completion.get("edge_uuids") or []) if str(uuid)]
        stamp_kwargs: dict[str, object] = {}
        if ctx.claimed.get("bootstrap_scope") is not None:
            stamp_kwargs["bootstrap_scope"] = ctx.claimed.get("bootstrap_scope")
        stamped = ctx.graph_adapter.stamp_ingest_metadata(
            node_uuids=[resolved_episode_uuid] + entity_uuids,
            edge_uuids=edge_uuids,
            session_id=str(ctx.claimed.get("session_id") or ""),
            user_id=str(ctx.claimed.get("user_id") or ""),
            source=str(ctx.claimed.get("source") or "claude-code"),
            source_confidence=source_confidence_for(str(ctx.claimed.get("source") or "claude-code")),
            namespace=str(ctx.claimed.get("namespace") or "default"),
            **stamp_kwargs,
        )
        if bool(ctx.claimed.get("user_flagged", False)):
            propagate_user_flag(
                ctx.graph_adapter,
                entity_uuids,
                episode_uuid=ctx.episode_uuid,
            )
        marked_ready = ctx.graph_adapter.mark_episode_ready(
            ctx.episode_uuid,
            worker_id=ctx.worker_id,
            resolved_episode_uuid=resolved_episode_uuid,
            nodes_touched=stamped.nodes_touched,
            edges_touched=stamped.edges_touched,
        )
        if not marked_ready:
            logger.info(
                "Skipping completion reconciliation after ownership lost episode_id=%s worker=%s",
                ctx.episode_uuid,
                ctx.worker_id,
            )
            return True
        record_lifecycle_event(
            component="ingest_worker",
            event="episode_ready",
            state="completed",
            episode_uuid=ctx.episode_uuid,
            details={
                "resolved_episode_uuid": resolved_episode_uuid,
                "nodes_touched": stamped.nodes_touched,
                "edges_touched": stamped.edges_touched,
                "reconciled_existing_completion": True,
            },
        )
        await emit_scheduler_task_event(
            parent_job_id=ctx.episode_uuid,
            parent_label=str(ctx.claimed.get("name") or ctx.episode_uuid),
            parent_state="ready",
            parent_heartbeat_at=datetime.now(timezone.utc).isoformat(),
            parent_metadata=build_episode_parent_metadata(
                attempts=ctx.processing_attempts,
                source=str(ctx.claimed.get("source") or ""),
                content=str(ctx.claimed.get("content") or ""),
                name=str(ctx.claimed.get("name") or ctx.episode_uuid),
            ),
        )
        duration_ms = int((perf_counter() - ctx.started) * 1000)
        record_mcp_event(
            kind="background",
            operation="episode_enrichment",
            payload={
                "episode_uuid": ctx.episode_uuid,
                "processing_attempts": ctx.processing_attempts,
                "queue_depth": ctx.get_queue_depth(),
            },
            result={
                "resolved_episode_uuid": resolved_episode_uuid,
                "nodes_touched": stamped.nodes_touched,
                "edges_touched": stamped.edges_touched,
                "reconciled_existing_completion": True,
            },
            duration_ms=duration_ms,
            success=True,
        )
        return True
    return False


# ---------------------------------------------------------------------------
# Pipeline step 2 — preflight rejection for oversized episodes
# ---------------------------------------------------------------------------

async def run_preflight_rejection(ctx: EnrichmentContext) -> bool:
    """Reject oversized episodes before Graphiti. Returns True if rejected."""

    preflight_rejection = build_episode_preflight_rejection(
        compose_episode_body(ctx.claimed),
        ctx.graphiti_episode_max_estimated_tokens,
    )
    if preflight_rejection is not None:
        # PART 2: Create raw-capture for terminal breakage (preflight oversize rejection).
        # This is best-effort — capture failure must not break the failure handling itself.
        try:
            episode_content = compose_episode_body(ctx.claimed)
            if episode_content.strip():
                # Short name: first ~60 chars of content
                capture_name = episode_content[:60].replace("\n", " ").strip()
                ctx.graph_adapter.create_raw_capture_entity(
                    episode_uuid=ctx.episode_uuid,
                    name=capture_name,
                    content=episode_content,
                    namespace=str(ctx.claimed.get("namespace") or "default"),
                    session_id=str(ctx.claimed.get("session_id") or ""),
                    user_id=str(ctx.claimed.get("user_id") or ""),
                    source=str(ctx.claimed.get("source") or "claude-code"),
                )
        except Exception as e:
            logger.debug(
                "Failed to create raw-capture for oversized episode %s: %s",
                ctx.episode_uuid,
                e,
            )

        failed = ctx.graph_adapter.mark_episode_failed(
            ctx.episode_uuid,
            str(preflight_rejection["error"]),
            worker_id=ctx.worker_id,
        )
        if not failed:
            logger.info(
                "Skipping oversized preflight failure write after ownership lost episode_id=%s worker=%s",
                ctx.episode_uuid,
                ctx.worker_id,
            )
            return True
        duration_ms = int((perf_counter() - ctx.started) * 1000)
        record_mcp_event(
            kind="background",
            operation="episode_enrichment",
            payload={
                "episode_uuid": ctx.episode_uuid,
                "processing_attempts": ctx.processing_attempts,
                "queue_depth": ctx.get_queue_depth(),
            },
            duration_ms=duration_ms,
            success=False,
            error=str(preflight_rejection["error"]),
        )
        record_failure_event(
            operation="episode_enrichment",
            episode_uuid=ctx.episode_uuid,
            failure_stage="graphiti_preflight_rejected",
            classification="terminal",
            retryable=False,
            processing_attempt=ctx.processing_attempts,
            queue_depth=ctx.get_queue_depth(),
            worker_id=ctx.worker_id,
            error_type=str(preflight_rejection["code"]),
            error=str(preflight_rejection["error"]),
            details={
                "source": ctx.claimed.get("source"),
                "session_id": ctx.claimed.get("session_id"),
                "user_id": ctx.claimed.get("user_id"),
                "duration_ms": duration_ms,
                "estimated_tokens": preflight_rejection["estimated_tokens"],
                "limit": preflight_rejection["limit"],
                "char_count": preflight_rejection["char_count"],
            },
        )
        record_lifecycle_event(
            component="ingest_worker",
            event="episode_preflight_rejected",
            state="failed",
            episode_uuid=ctx.episode_uuid,
            details={
                "estimated_tokens": preflight_rejection["estimated_tokens"],
                "limit": preflight_rejection["limit"],
                "char_count": preflight_rejection["char_count"],
            },
        )
        await emit_scheduler_task_event(
            parent_job_id=ctx.episode_uuid,
            parent_label=str(ctx.claimed.get("name") or ctx.episode_uuid),
            parent_state="failed",
            parent_heartbeat_at=datetime.now(timezone.utc).isoformat(),
            parent_error=str(preflight_rejection["error"]),
            parent_metadata=build_episode_parent_metadata(
                attempts=ctx.processing_attempts,
                source=str(ctx.claimed.get("source") or ""),
                content=str(ctx.claimed.get("content") or ""),
                name=str(ctx.claimed.get("name") or ctx.episode_uuid),
            ),
        )
        logger.warning(
            "Rejected oversized episode before Graphiti extraction episode_id=%s estimated_tokens=%s limit=%s",
            ctx.episode_uuid,
            preflight_rejection["estimated_tokens"],
            preflight_rejection["limit"],
        )
        return True
    return False


# ---------------------------------------------------------------------------
# Pipeline step 3 — Graphiti extraction (under ingest lock)
# ---------------------------------------------------------------------------

async def run_graphiti_extraction(
    ctx: EnrichmentContext,
    *,
    finalize_under_gate: bool,
) -> Any:
    """Acquire the ingest gate and run the Graphiti add_episode pipeline.

    The gate bounds total concurrent extractions and serializes per ``group_id``
    (namespace), so episodes in different namespaces extract in parallel while
    same-namespace episodes never race on entity dedup. Production workers set
    ``finalize_under_gate`` so correlation and graph-mutating finalization remain
    in that same namespace-critical section. Extraction-only tests opt out
    explicitly so a production caller cannot accidentally skip the wider gate.
    """

    from menhir.domain.namespace import namespace_to_group_id

    namespace = str(ctx.claimed.get("namespace") or "default")
    group_id = namespace_to_group_id(namespace if namespace != "default" else None)

    stamped_ok = ctx.graph_adapter.update_episode_processing(
        ctx.episode_uuid,
        worker_id=ctx.worker_id,
        stage="graphiti_extracting",
        substage="awaiting_graphiti_response",
        progress=20.0,
        steps_total=ctx.processing_steps_total,
        steps_completed=1,
        llm_active_task="memory: graphiti add_episode",
    )
    if not stamped_ok:
        # CF-233: the stamp did not apply. The sibling terminal writes
        # (mark_episode_ready/mark_episode_failed) have the identical bool contract and
        # every caller checks it; these five discarded it, so a worker whose lease had
        # already gone kept running the pipeline -- LLM calls included -- until the
        # terminal write finally refused.
        #
        # Reported, NOT acted on, because False is ambiguous: episode_stamping returns
        # it for lost ownership, for a missing node, AND for a call with no fields to
        # set. Treating it as proof of ownership loss would repeat CF-205 exactly.
        logger.warning(
            "Episode progress stamp did not apply episode_id=%s worker=%s; "
            "the episode is no longer owned by this worker or no longer exists",
            ctx.episode_uuid,
            ctx.worker_id,
        )
    record_lifecycle_event(
        component="ingest_worker",
        event="graphiti_extracting",
        state="started",
        episode_uuid=ctx.episode_uuid,
    )
    async with ctx.ingest_gate.acquire(group_id):
        logger.info("Episode enrichment acquired ingest gate episode_id=%s group_id=%s", ctx.episode_uuid, group_id)
        record_lifecycle_event(
            component="ingest_worker",
            event="ingest_lock",
            state="acquired",
            episode_uuid=ctx.episode_uuid,
        )
        record_lifecycle_event(
            component="ingest_worker",
            event="emit_parent_job_trace",
            state="started",
            episode_uuid=ctx.episode_uuid,
            details={"name": str(ctx.claimed.get("name") or ctx.episode_uuid)},
        )
        try:
            await emit_scheduler_task_event(
                parent_job_id=ctx.episode_uuid,
                parent_label=str(ctx.claimed.get("name") or ctx.episode_uuid),
                parent_state="graphiti_extracting",
                parent_heartbeat_at=datetime.now(timezone.utc).isoformat(),
                parent_metadata=build_episode_parent_metadata(
                    attempts=ctx.processing_attempts,
                    source=str(ctx.claimed.get("source") or ""),
                    content=str(ctx.claimed.get("content") or ""),
                    name=str(ctx.claimed.get("name") or ctx.episode_uuid),
                ),
            )
        except Exception as exc:
            record_lifecycle_event(
                component="ingest_worker",
                event="emit_parent_job_trace",
                state="failed",
                episode_uuid=ctx.episode_uuid,
                details={
                    "name": str(ctx.claimed.get("name") or ctx.episode_uuid),
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
            raise
        else:
            record_lifecycle_event(
                component="ingest_worker",
                event="emit_parent_job_trace",
                state="completed",
                episode_uuid=ctx.episode_uuid,
                details={"name": str(ctx.claimed.get("name") or ctx.episode_uuid)},
            )
        record_lifecycle_event(
            component="ingest_worker",
            event="before_add_episode_timeout_wrapper",
            state="started",
            episode_uuid=ctx.episode_uuid,
            details={"name": str(ctx.claimed.get("name") or ctx.episode_uuid)},
        )
        record_lifecycle_event(
            component="ingest_worker",
            event="dispatch_add_episode_timeout_wrapper",
            state="started",
            episode_uuid=ctx.episode_uuid,
            details={"name": str(ctx.claimed.get("name") or ctx.episode_uuid)},
        )

        # Stage 1 shadow-mode context composition (observe-only): candidate retrieval MUST
        # happen here, before the real extraction call, so this episode's own about-to-be-
        # created facts cannot leak into its own candidate pool. It is cheap and read-only,
        # so it's safe to run inside the gate — the expensive LLM work happens later, after
        # the gate is released (see the dispatch below). shadow_candidates/shadow_candidate_
        # error/shadow_retrieval_ms are all None/empty/0 when the flag is off (zero overhead).
        shadow_candidates: list = []
        shadow_candidate_error: str | None = None
        shadow_retrieval_ms = 0
        if ctx.shadow_context_composition:
            shadow_retrieval_started = perf_counter()
            try:
                shadow_namespace = str(ctx.claimed.get("namespace") or "default")
                shadow_candidates, shadow_candidate_error = await snapshot_candidate_facts(
                    ctx.graphiti_client, ctx.graph_adapter,
                    namespace=shadow_namespace, episode_body=compose_episode_body(ctx.claimed),
                )
            except Exception as exc:  # snapshot_candidate_facts is itself fail-safe; this is
                # a last-resort net (also covers the namespace/body prep above) so a shadow
                # bug can never block real extraction or leave the surrounding lifecycle
                # events (started at line ~528) without a matching outcome.
                logger.debug("Shadow candidate snapshot raised unexpectedly episode_id=%s", ctx.episode_uuid, exc_info=True)
                shadow_candidates, shadow_candidate_error = [], str(exc)
            shadow_retrieval_ms = int((perf_counter() - shadow_retrieval_started) * 1000)

        try:
            turn_evidence_uuid = str(
                ctx.claimed.get("turn_evidence_uuid") or ""
            ).strip()
            relationless_repair_context_loader: Callable[[], tuple[str, ...]] | None = None
            if turn_evidence_uuid:
                repair_namespace = normalize_logical_namespace(ctx.claimed.get("namespace"))

                def _load_relationless_repair_context() -> tuple[str, ...]:
                    # turn_evidence_uuid is caller-supplied. Scoping the read to THIS episode's
                    # namespace is what stops a foreign turn's text entering this extraction
                    # (CF-236); the admission gate governs trust tier, not this path.
                    rows = ctx.graph_adapter.load_preceding_turn_evidence_context(
                        turn_evidence_uuid,
                        namespace=repair_namespace,
                        limit=2,
                    )
                    return tuple(
                        f"{str(row.get('role') or '').strip().lower()}: "
                        f"{str(row.get('text') or '').strip()}"
                        for row in rows
                        if str(row.get("role") or "").strip()
                        and str(row.get("text") or "").strip()
                    )

                relationless_repair_context_loader = _load_relationless_repair_context

            episode_name = str(ctx.claimed.get("name") or ctx.episode_uuid)
            source_description = str(ctx.claimed.get("source") or "claude-code")
            reference_time = coerce_reference_time(
                ctx.claimed.get("reference_time") or ctx.claimed.get("queued_at")
            )
            publication_intent = None
            if ctx.evidence_publication_intents is not None:
                publication_intent = ctx.evidence_publication_intents.begin(
                    episode_uuid=ctx.episode_uuid,
                    namespace=namespace,
                    expected_name=episode_name,
                    source_description=source_description,
                    reference_time=reference_time,
                )
                if not publication_intent.dispatch_allowed:
                    raise PublicationDispatchSuppressed(
                        "Graphiti dispatch refused because publication intent "
                        f"{publication_intent.intent_key!r} is already "
                        f"{publication_intent.status}"
                    )

            self_bind_mode = resolve_bind_mode(
                getattr(ctx, "canonical_self_binding_mode", None)
            )
            self_identity = self_context_for_pending_episode(
                source=ctx.claimed.get("source"),
                namespace=namespace,
                episode_uuid=ctx.episode_uuid,
                turn_evidence_uuid=str(
                    ctx.claimed.get("turn_evidence_uuid") or ""
                ).strip() or None,
                principal_id=str(ctx.claimed.get("user_id") or "").strip() or None,
            )
            # Observe must preserve the real extraction prompt byte-for-byte.  The endpoint is a
            # behavioral input, so only enforce constructs and transports it; off/observe keep the
            # established extraction path while the existing decision telemetry remains available.
            self_subject_endpoint = (
                self_subject_endpoint_for_claim(ctx.claimed)
                if self_bind_mode is SelfBindMode.ENFORCE
                else None
            )
            self_assertion_authorizer = None
            if self_subject_endpoint is not None:
                # Structural identity is established from the trusted claim, independently of
                # extracted prose. This does not authorize any semantic edge; the final-payload
                # verifier below still requires an exact owner signature for each assertion.
                ensured_self_uuid = str(ctx.graph_adapter.ensure_self_entity(namespace) or "")
                if ensured_self_uuid != self_identity.self_uuid:
                    raise InvalidSelfSubjectDeclarationError(
                        "canonical self repository returned an unexpected namespace identity"
                    )
                self_assertion_authorizer = FileSelfAssertionAuthorizer(
                    public_key_path=ctx.canonical_self_confirmation_public_key_path,
                    public_key_sha256=ctx.canonical_self_confirmation_public_key_sha256,
                    confirmation_directory=ctx.canonical_self_confirmation_directory,
                )

            graphiti_result = await add_episode_with_timeout(
                ctx.graphiti_client,
                name=episode_name,
                episode_body=compose_episode_body(ctx.claimed),
                source_description=source_description,
                reference_time=reference_time,
                episode_uuid=ctx.episode_uuid,
                attempt=ctx.processing_attempts,
                timeout_s=ctx.graphiti_add_episode_timeout_s,
                group_id=group_id,
                relationless_repair_context_loader=relationless_repair_context_loader,
                self_identity=self_identity,
                self_subject_endpoint=self_subject_endpoint,
                self_bind_mode=self_bind_mode,
                self_assertion_authorizer=self_assertion_authorizer,
            )
            if publication_intent is not None:
                publication_transition = (
                    ctx.evidence_publication_intents.finalize_remote_outcome(
                        publication_intent,
                        remote_episode_uuid=str(graphiti_result.episode.uuid),
                    )
                )
                if not publication_transition.finalized:
                    logger.warning(
                        "Graphiti evidence quarantined episode_id=%s remote_episode_id=%s "
                        "reason=%s candidates=%s tombstones=%s",
                        ctx.episode_uuid,
                        graphiti_result.episode.uuid,
                        publication_transition.reason,
                        publication_transition.candidate_count,
                        publication_transition.tombstone_count,
                    )
        except Exception:  # re-raised; record telemetry for any graphiti failure
            record_lifecycle_event(
                component="ingest_worker",
                event="dispatch_add_episode_timeout_wrapper",
                state="failed",
                episode_uuid=ctx.episode_uuid,
                details={"name": str(ctx.claimed.get("name") or ctx.episode_uuid)},
            )
            raise
        else:
            record_lifecycle_event(
                component="ingest_worker",
                event="dispatch_add_episode_timeout_wrapper",
                state="completed",
                episode_uuid=ctx.episode_uuid,
                details={"name": str(ctx.claimed.get("name") or ctx.episode_uuid)},
            )
        finally:
            record_lifecycle_event(
                component="ingest_worker",
                event="dispatch_add_episode_timeout_wrapper",
                state="finally",
                episode_uuid=ctx.episode_uuid,
                details={"name": str(ctx.claimed.get("name") or ctx.episode_uuid)},
            )
        logger.info("Episode enrichment graphiti call returned episode_id=%s", ctx.episode_uuid)
        record_lifecycle_event(
            component="ingest_worker",
            event="before_add_episode_timeout_wrapper",
            state="completed",
            episode_uuid=ctx.episode_uuid,
        )
        record_lifecycle_event(
            component="ingest_worker",
            event="graphiti_add_episode",
            state="completed",
            episode_uuid=ctx.episode_uuid,
        )
        if finalize_under_gate:
            await stamp_and_finalize(ctx, graphiti_result)
    # Gate released above (the `async with` block ended). Shadow processing dispatches here,
    # AFTER release, specifically so it never adds to same-namespace queue latency. It's a
    # detached background task, not awaited — real episode completion has already finished
    # when ``finalize_under_gate`` is enabled and never waits on shadow latency.
    if ctx.shadow_context_composition:
        _dispatch_shadow_composition(
            ctx,
            candidates=shadow_candidates,
            candidate_error=shadow_candidate_error,
            candidate_retrieval_ms=shadow_retrieval_ms,
            graphiti_result=graphiti_result,
        )
    return graphiti_result


def _dispatch_shadow_composition(
    ctx: EnrichmentContext,
    *,
    candidates: list,
    candidate_error: str | None,
    candidate_retrieval_ms: int,
    graphiti_result: Any,
) -> None:
    """Fire-and-forget: builds and logs the shadow trace without blocking the caller.
    Registered with IngestService's task-tracking set (via ctx.register_background_task)
    when a real IngestService is behind this ctx, so shutdown() can drain it cleanly;
    None (tests constructing EnrichmentContext directly) just means "don't track"."""
    task = asyncio.create_task(
        _run_shadow_composition_and_log(
            ctx, candidates=candidates, candidate_error=candidate_error,
            candidate_retrieval_ms=candidate_retrieval_ms, graphiti_result=graphiti_result,
        ),
        name=f"menhir-shadow-composition-{ctx.episode_uuid}",
    )
    if ctx.register_background_task is not None:
        ctx.register_background_task(task)


async def _run_shadow_composition_and_log(
    ctx: EnrichmentContext,
    *,
    candidates: list,
    candidate_error: str | None,
    candidate_retrieval_ms: int,
    graphiti_result: Any,
) -> None:
    shadow_started = perf_counter()
    try:
        prediction = await run_shadow_composition_with_timeout(
            ctx.llm,
            episode_uuid=ctx.episode_uuid,
            namespace=str(ctx.claimed.get("namespace") or "default"),
            episode_body=compose_episode_body(ctx.claimed),
            reference_time=coerce_reference_time(
                ctx.claimed.get("reference_time") or ctx.claimed.get("queued_at")
            ).isoformat(),
            candidates=candidates,
            candidate_query_error=candidate_error,
            candidate_retrieval_ms=candidate_retrieval_ms,
            timeout_s=ctx.shadow_composition_timeout_s,
        )
        shadow_total_ms = int((perf_counter() - shadow_started) * 1000)
        trace = build_shadow_trace(prediction, graphiti_result, shadow_total_ms=shadow_total_ms)
        record_lifecycle_event(
            component="ingest_shadow",
            event="extraction_composition",
            state="logged",
            episode_uuid=ctx.episode_uuid,
            details=shadow_trace_to_details(trace),
        )
    except Exception:
        # This function is never awaited by the real ingest path — a bug here must not
        # become an unretrieved-task-exception warning at GC time, and must never be
        # mistaken for a real extraction failure.
        logger.warning("Shadow composition logging failed episode_id=%s", ctx.episode_uuid, exc_info=True)


# ---------------------------------------------------------------------------
# Edge fact repair — best-effort LLM rewrite of synthetic facts
# ---------------------------------------------------------------------------

async def _repair_synthetic_edge_facts(
    ctx: EnrichmentContext,
    extracted_edges: list[Any],
    episode_content: str,
) -> None:
    """Best-effort LLM repair of edges with synthetic facts.

    Identifies edges whose ``fact`` starts with the synthetic prefix,
    calls the LLM to produce a proper fact, and writes the result back
    to Neo4j with provenance tracking.
    """
    if ctx.llm is None or not extracted_edges:
        return

    synthetic_edges: list[tuple[int, Any]] = []
    for i, edge in enumerate(extracted_edges):
        fact = getattr(edge, "fact", "") or ""
        if fact.startswith(SYNTHETIC_FACT_PREFIX):
            synthetic_edges.append((i, edge))

    if not synthetic_edges:
        # All facts are original — stamp provenance for all edges
        updates = []
        for e in extracted_edges:
            uuid = getattr(e, "uuid", None)
            fact = getattr(e, "fact", None)
            if uuid and fact:
                updates.append({"uuid": uuid, "fact": fact, "fact_source": "original"})
        if updates:
            ctx.graph_adapter.update_edge_facts(updates)
        return

    # Build stubs for LLM repair
    stubs: list[dict[str, str]] = []
    for _, edge in synthetic_edges:
        stubs.append({
            "source": getattr(edge, "name", "").split(" -> ")[0] if " -> " in getattr(edge, "name", "") else str(getattr(edge, "source_node_uuid", "")),
            "target": getattr(edge, "name", "").split(" -> ")[-1] if " -> " in getattr(edge, "name", "") else str(getattr(edge, "target_node_uuid", "")),
            "relation": getattr(edge, "name", "") or "related_to",
        })

    try:
        repaired = await ctx.llm.repair_edge_facts(episode_content, stubs)
    except Exception:
        logger.debug("Edge fact repair LLM call failed", exc_info=True)
        repaired = [None] * len(synthetic_edges)

    # Build bulk update list
    updates: list[dict[str, str]] = []
    synthetic_idx_set = {i for i, _ in synthetic_edges}
    for idx, edge in enumerate(extracted_edges):
        uuid = getattr(edge, "uuid", None)
        fact = getattr(edge, "fact", None)
        if uuid and fact and idx not in synthetic_idx_set:
            updates.append({"uuid": uuid, "fact": fact, "fact_source": "original"})

    for j, (_, edge) in enumerate(synthetic_edges):
        uuid = getattr(edge, "uuid", None)
        if not uuid:
            continue
        repaired_fact = repaired[j] if j < len(repaired) else None
        # CF-78: between the model returning a string and that string becoming a stored edge fact
        # there used to be exactly one check -- `if repaired_fact:`. The input to that model is
        # untrusted episode content, and the output is persisted into a graph recall renders
        # verbatim into an operator's agent context (CF-39). Truthiness is not validation.
        #
        # `fact_source` is what decides how hard to look. That field was written at five sites and
        # read at none, which made it a provenance marker that recorded a distinction nothing
        # honoured; branching on it here is the first consumer. An `original` fact came from the
        # extractor and keeps its existing treatment. An `llm_repaired` one is model prose about
        # attacker-influenced text and is held to the stricter bar.
        if repaired_fact and _is_admissible_repaired_fact(repaired_fact):
            updates.append({"uuid": uuid, "fact": repaired_fact, "fact_source": "llm_repaired"})
        else:
            updates.append({
                "uuid": uuid,
                "fact": strip_synthetic_prefix(getattr(edge, "fact", "") or ""),
                "fact_source": "synthetic_fallback",
            })

    if updates:
        ctx.graph_adapter.update_edge_facts(updates)

    repaired_count = sum(1 for j, _ in enumerate(synthetic_edges) if j < len(repaired) and repaired[j])
    logger.info(
        "Edge fact repair episode_id=%s: %d synthetic, %d repaired, %d fallback",
        ctx.episode_uuid,
        len(synthetic_edges),
        repaired_count,
        len(synthetic_edges) - repaired_count,
    )


# ---------------------------------------------------------------------------
# Structural anchoring (best-effort, non-fatal)
# ---------------------------------------------------------------------------

#: An edge fact is a short declarative sentence about two entities. These are the bounds a real
#: one stays inside; anything outside them is not a fact this pipeline produced.
_MAX_REPAIRED_FACT_CHARS = 500
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _is_admissible_repaired_fact(candidate: object) -> bool:
    """Whether a model-repaired edge fact may be persisted (CF-78).

    Deliberately a SHAPE check, not a semantic one. The register's ideal -- verify the fact is
    supported by the source span -- is a grounding test, and CF-17 is the standing record of how
    badly a naive grounding test goes: token overlap admitted every single-word contradiction of
    its source, and the substring replacement admitted quotation, attribution and conditionals as
    assertions. Shipping a second one of those here, to guard a lower-severity path, would be
    repeating a mistake this codebase has already made twice and written down.

    So: bound what can be said, and leave what it means to the pipeline that already judges it.

    - a length bound, because an edge fact is a sentence and anything at 500+ characters is a
      payload wearing a sentence's clothes;
    - no control characters or line breaks, which are what let stored text stop being one field
      and start looking like structure when it is rendered into a later prompt or an agent's
      context (CF-39 is that delivery site, and it is confirmed);
    - non-empty after stripping, which the old truthiness check nearly covered and did not, since
      a whitespace-only string is truthy.

    What this does NOT do is check that the fact is true, or that the model was not steered into
    writing it. A short, clean, well-formed lie passes. That is the honest boundary of a shape
    check and the reason `fact_source` still marks these as `llm_repaired`.
    """
    if not isinstance(candidate, str):
        return False
    text = candidate.strip()
    if not text or len(text) > _MAX_REPAIRED_FACT_CHARS:
        return False
    if "\n" in candidate or "\r" in candidate:
        return False
    return not _CONTROL_CHARS_RE.search(candidate)


def _anchor_to_structural_entities(
    ctx: EnrichmentContext,
    extracted_node_uuids: list[str],
    episode_body: str,
) -> dict[str, int]:
    """Best-effort structural anchoring with narrative/diff provenance.

    Creates ANCHORED_TO edges from extracted semantic entities to structural
    file entities referenced in the episode body. Narrative-mentioned files
    get weight 1.0; diff-only files get weight 0.3.
    """
    from menhir.infrastructure.structural_anchoring import (
        extract_file_paths,
        normalize_to_repo_relative,
        split_narrative_and_diff,
    )

    counts: dict[str, int] = {"narrative": 0, "diff": 0}

    if not extracted_node_uuids:
        return counts

    try:
        narrative, diff = split_narrative_and_diff(episode_body)

        narrative_paths = extract_file_paths(narrative)
        diff_paths = extract_file_paths(diff)
        # Remove paths already in narrative (avoid double-linking)
        narrative_set = set(narrative_paths)
        diff_only_paths = [p for p in diff_paths if p not in narrative_set]

        if narrative_paths:
            normalized = [normalize_to_repo_relative(p) for p in narrative_paths]
            counts["narrative"] = ctx.graph_adapter.anchor_semantic_to_structural(
                extracted_node_uuids, normalized,
                anchor_source="narrative_path", weight=1.0,
            )

        if diff_only_paths:
            normalized = [normalize_to_repo_relative(p) for p in diff_only_paths]
            counts["diff"] = ctx.graph_adapter.anchor_semantic_to_structural(
                extracted_node_uuids, normalized,
                anchor_source="diff_path", weight=0.3,
            )

        total = counts["narrative"] + counts["diff"]
        if total > 0:
            logger.info(
                "Structural anchoring created %d edges (narrative=%d, diff=%d) episode=%s",
                total, counts["narrative"], counts["diff"], ctx.episode_uuid,
            )
    except Exception:
        logger.debug("Structural anchoring failed (non-fatal) episode=%s", ctx.episode_uuid, exc_info=True)

    return counts


# ---------------------------------------------------------------------------
# Pipeline step 4 — stamp metadata, rehydrate, and finalize
# ---------------------------------------------------------------------------

async def stamp_and_finalize(
    ctx: EnrichmentContext,
    graphiti_result: Any,
) -> None:
    """Stamp metadata, rehydrate compressed nodes, and mark episode ready."""

    stamped_ok = ctx.graph_adapter.update_episode_processing(
        ctx.episode_uuid,
        worker_id=ctx.worker_id,
        stage="stamping",
        substage="graphiti_response_received",
        progress=55.0,
        steps_total=ctx.processing_steps_total,
        steps_completed=2,
        clear_llm_active=True,
    )
    if not stamped_ok:
        # CF-233: the stamp did not apply. The sibling terminal writes
        # (mark_episode_ready/mark_episode_failed) have the identical bool contract and
        # every caller checks it; these five discarded it, so a worker whose lease had
        # already gone kept running the pipeline -- LLM calls included -- until the
        # terminal write finally refused.
        #
        # Reported, NOT acted on, because False is ambiguous: episode_stamping returns
        # it for lost ownership, for a missing node, AND for a call with no fields to
        # set. Treating it as proof of ownership loss would repeat CF-205 exactly.
        logger.warning(
            "Episode progress stamp did not apply episode_id=%s worker=%s; "
            "the episode is no longer owned by this worker or no longer exists",
            ctx.episode_uuid,
            ctx.worker_id,
        )

    resolved_episode_uuid = graphiti_result.episode.uuid
    extracted_nodes = graphiti_result.nodes
    extracted_edges = graphiti_result.edges
    extracted_episodic_edges = graphiti_result.episodic_edges

    # Consume the combined-extraction receipt exactly once (populated inside Graphiti's
    # child task by the sanitation validator). It lets us tell a legitimate empty
    # extraction apart from a collapse where the LLM extracted content that resolution
    # then dropped. Read + clear here regardless of which branch we take below.
    receipt = get_extraction_receipt()
    clear_extraction_receipt()
    raw_extraction_nonempty = (
        receipt is not None
        and receipt.episode_key == ctx.episode_uuid
        and (receipt.raw_entity_count > 0 or receipt.raw_edge_count > 0)
    )

    if not still_owns_episode(ctx.graph_adapter, ctx.episode_uuid, ctx.worker_id):
        logger.info(
            "Skipping enrichment completion after ownership lost episode_id=%s worker=%s",
            ctx.episode_uuid,
            ctx.worker_id,
        )
        return

    if (
        receipt is not None
        and receipt.episode_key == ctx.episode_uuid
        and receipt.self_subject_endpoint is not None
        and receipt.self_bind_mode is SelfBindMode.ENFORCE
    ):
        proposals_recorded = ctx.graph_adapter.record_self_assertion_proposals(
            ctx.episode_uuid,
            worker_id=ctx.worker_id,
            proposals=list(receipt.self_assertion_proposals or []),
            authorized_count=receipt.self_assertions_authorized,
            policy_version=SELF_ASSERTION_POLICY_VERSION,
        )
        if not proposals_recorded:
            logger.info(
                "Skipping enrichment completion after self-proposal receipt lost ownership "
                "episode_id=%s worker=%s",
                ctx.episode_uuid,
                ctx.worker_id,
            )
            return

    if not extracted_nodes and not extracted_edges:
        if raw_extraction_nonempty and is_policy_empty_extraction(receipt):
            # Policy-empty: the extraction produced entities but no persistable content.
            # Cases: (a) assistant turn with only self-label and no relationship, (b) every
            # usable edge was user->X echo suppressed by design, (c) any-source turn where
            # BOTH the initial and the repair extraction produced only self-labels with zero
            # edges (e.g. evidence projection of "Thanks again for your help!").
            logger.info(
                "Policy-empty enrichment (success) episode_id=%s "
                "(raw_edges=%d suppressed=%d self_only_relationless=%s "
                "initial_self_only=%s repair_self_only=%s repair_attempted=%s "
                "context_unsupported_edges_suppressed=%d)",
                ctx.episode_uuid,
                receipt.raw_edge_count,
                receipt.self_echo_edges_suppressed,
                receipt.assistant_self_only_relationless,
                receipt.initial_self_only_entities,
                receipt.repair_self_only_entities,
                receipt.relationless_repair_attempted,
                receipt.context_unsupported_edges_suppressed,
            )
        elif raw_extraction_nonempty:
            # Collapse: the LLM DID return entities/edges but resolution persisted nothing.
            # This is a linkage/provenance failure, not an empty episode — surface it as an
            # explicit retryable failure instead of masking it as "zero-extraction success"
            # (which would permanently lose the content). Enters the standard failure path.
            #
            # `relationless` names the sub-case where the model returned entities and NO edge at all
            # even after the combined extractor's one bounded corrective pass. The raw shape alone
            # does NOT prove the source text stated no relation: gpt-4o-mini repeatedly returned
            # `new app` with no edge for "I'm actually using a new app I recently downloaded."
            # The wrapper now repairs that under-extraction once with a focused prompt. If it still
            # cannot produce a grounded relationship, the episode remains a visible non-retryable
            # failure rather than burning the scheduler retry budget or silently losing content.
            # A titled list no longer reaches here: sanitation emits the membership its syntax states.
            relationless = receipt.raw_entity_count > 0 and receipt.raw_edge_count == 0
            raise CombinedExtractionCollapsedError(
                ("relationless_extraction " if relationless else "combined_extraction_collapsed ")
                + f"episode_id={ctx.episode_uuid} "
                f"retryable={'false' if relationless else 'true'} "
                f"raw_entities={receipt.raw_entity_count} "
                f"raw_edges={receipt.raw_edge_count} "
                f"list_membership_edges_added={receipt.list_membership_edges_added} "
                f"malformed_entities_dropped={receipt.malformed_entities_dropped} "
                f"malformed_edges_dropped={receipt.malformed_edges_dropped} "
                f"endpoints_synthesized={receipt.endpoints_synthesized} "
                f"orphan_nodes_dropped={receipt.orphan_nodes_dropped} "
                f"relationless_repair_attempted={str(receipt.relationless_repair_attempted).lower()} "
                f"relationless_repair_succeeded={str(receipt.relationless_repair_succeeded).lower()} "
                f"assistant_self_only_relationless="
                f"{str(receipt.assistant_self_only_relationless).lower()} "
                f"initial_self_only_entities="
                f"{str(receipt.initial_self_only_entities).lower()} "
                f"repair_self_only_entities="
                f"{str(receipt.repair_self_only_entities).lower()} "
                f"context_unsupported_edges_suppressed="
                f"{receipt.context_unsupported_edges_suppressed} "
                "resolved_nodes=0 resolved_edges=0"
            )
        # PART 1: Zero-extraction is a successful empty determination, not a failure.
        # The episode may have had no memorable content (e.g., an "ok thanks" response),
        # which is a valid outcome, not a breakage condition.
        logger.info(
            "Zero-extraction enrichment (success) episode_id=%s — Graphiti returned no nodes or edges",
            ctx.episode_uuid,
        )
        marked_ready = ctx.graph_adapter.mark_episode_ready(
            ctx.episode_uuid,
            worker_id=ctx.worker_id,
            resolved_episode_uuid=resolved_episode_uuid,
            nodes_touched=0,
            edges_touched=0,
        )
        if not marked_ready:
            logger.info(
                "Skipping zero-extraction ready write after ownership lost episode_id=%s worker=%s",
                ctx.episode_uuid,
                ctx.worker_id,
            )
            return
        duration_ms = int((perf_counter() - ctx.started) * 1000)
        # Record as successful empty extraction with a reason receipt
        record_mcp_event(
            kind="background",
            operation="episode_enrichment",
            payload={
                "episode_uuid": ctx.episode_uuid,
                "processing_attempts": ctx.processing_attempts,
                "queue_depth": ctx.get_queue_depth(),
            },
            result={"empty_extraction": True},
            duration_ms=duration_ms,
            success=True,
        )
        # DO NOT emit record_failure_event — zero-extraction is not a failure
        # The _failed_enrichments counter is untouched (only incremented by caller on exceptions)
        record_lifecycle_event(
            component="ingest_worker",
            event="episode_empty",
            state="completed",
            episode_uuid=ctx.episode_uuid,
            details={"reason": "empty_extraction"},
        )
        await emit_scheduler_task_event(
            parent_job_id=ctx.episode_uuid,
            parent_label=str(ctx.claimed.get("name") or ctx.episode_uuid),
            parent_state="ready",
            parent_heartbeat_at=datetime.now(timezone.utc).isoformat(),
            parent_metadata=build_episode_parent_metadata(
                attempts=ctx.processing_attempts,
                source=str(ctx.claimed.get("source") or ""),
                content=str(ctx.claimed.get("content") or ""),
                name=str(ctx.claimed.get("name") or ctx.episode_uuid),
            ),
        )
        return

    node_uuids = [resolved_episode_uuid] + [node.uuid for node in extracted_nodes]
    edge_uuids = [edge.uuid for edge in extracted_edges] + [
        edge.uuid for edge in extracted_episodic_edges
    ]
    stamp_kwargs: dict[str, object] = {}
    if ctx.claimed.get("bootstrap_scope") is not None:
        stamp_kwargs["bootstrap_scope"] = ctx.claimed.get("bootstrap_scope")
    stamped = ctx.graph_adapter.stamp_ingest_metadata(
        node_uuids=node_uuids,
        edge_uuids=edge_uuids,
        session_id=str(ctx.claimed.get("session_id") or ""),
        user_id=str(ctx.claimed.get("user_id") or ""),
        source=str(ctx.claimed.get("source") or "claude-code"),
        source_confidence=source_confidence_for(str(ctx.claimed.get("source") or "claude-code")),
        namespace=str(ctx.claimed.get("namespace") or "default"),
        **stamp_kwargs,
    )
    if bool(ctx.claimed.get("user_flagged", False)):
        propagate_user_flag(
            ctx.graph_adapter,
            [node.uuid for node in extracted_nodes],
            episode_uuid=ctx.episode_uuid,
        )
    # M6 Phase 5: Record scope assignment for extracted entity nodes
    if ctx.settings_record_revisions:
        for node in extracted_nodes:
            record_memory_revision(
                node_uuid=node.uuid,
                field="scope",
                old_value=None,
                new_value="SESSION",
                changed_by="ingest",
                episode_uuid=ctx.episode_uuid,
            )
    stamped_ok = ctx.graph_adapter.update_episode_processing(
        ctx.episode_uuid,
        worker_id=ctx.worker_id,
        stage="post_process",
        substage="metadata_stamped",
        progress=75.0,
        steps_total=ctx.processing_steps_total,
        steps_completed=3,
    )
    if not stamped_ok:
        # CF-233: the stamp did not apply. The sibling terminal writes
        # (mark_episode_ready/mark_episode_failed) have the identical bool contract and
        # every caller checks it; these five discarded it, so a worker whose lease had
        # already gone kept running the pipeline -- LLM calls included -- until the
        # terminal write finally refused.
        #
        # Reported, NOT acted on, because False is ambiguous: episode_stamping returns
        # it for lost ownership, for a missing node, AND for a call with no fields to
        # set. Treating it as proof of ownership loss would repeat CF-205 exactly.
        logger.warning(
            "Episode progress stamp did not apply episode_id=%s worker=%s; "
            "the episode is no longer owned by this worker or no longer exists",
            ctx.episode_uuid,
            ctx.worker_id,
        )
    # Best-effort LLM repair of synthetic edge facts
    await _repair_synthetic_edge_facts(
        ctx, extracted_edges, compose_episode_body(ctx.claimed),
    )
    # Best-effort structural anchoring
    _anchor_to_structural_entities(
        ctx, [node.uuid for node in extracted_nodes], compose_episode_body(ctx.claimed),
    )
    # Best-effort semantic correlation check (Step 8)
    # After nodes are committed, check if they correlate with existing entities.
    # Correlations create RELATES_TO edges (0.7–0.85 sim) or trigger merge (>0.95 sim).
    # Conflict-range pairs (0.85–0.95) are returned for the lifecycle service to flag.
    correlation_conflicts = 0
    try:
        from menhir.services.correlation_service import CorrelationService
        correlation_service = CorrelationService(
            correlation_repo=ctx.graph_adapter._correlation,
            graphiti_client=ctx.graphiti_client,
            llm=ctx.llm,  # Part 2: Pass LLM for judge-gated merge
        )
        episode_body = compose_episode_body(ctx.claimed)
        # Part 1: namespace-scoped correlation search (deterministic veto gate)
        namespace = str(ctx.claimed.get("namespace") or "default")
        for node in extracted_nodes:
            # Part 5: kill the episode-body fallback — skip correlation for unnamed nodes
            node_name = str(getattr(node, "name", "") or "").strip()
            node_content = str(getattr(node, "content", "") or "").strip()
            if not node_name and not node_content:
                # Node has no identity claim — skip correlation
                continue
            node_query = node_name or node_content
            corr_result = await correlation_service.check_correlation(
                node.uuid, node_query, namespace=namespace,
            )
            correlation_conflicts += corr_result.conflicts
            if corr_result.related > 0 or corr_result.merged > 0:
                logger.info(
                    "Correlation check: node=%s related=%d merged=%d conflicts=%d",
                    node.uuid, corr_result.related, corr_result.merged, corr_result.conflicts,
                )
    except Exception:
        logger.warning("Correlation check failed (best-effort)", exc_info=True)
    if ctx.lifecycle_service is not None:
        entity_uuids = [node.uuid for node in extracted_nodes]
        freshness_map = ctx.graph_adapter.fetch_node_freshness(entity_uuids)
        episode_content = compose_episode_body(ctx.claimed)
        stamped_ok = ctx.graph_adapter.update_episode_processing(
            ctx.episode_uuid,
            worker_id=ctx.worker_id,
            stage="rehydrating",
            substage="checking_compressed_nodes",
            progress=85.0,
            steps_total=ctx.processing_steps_total,
            steps_completed=4,
        )
        if not stamped_ok:
            # CF-233: the stamp did not apply. The sibling terminal writes
            # (mark_episode_ready/mark_episode_failed) have the identical bool contract and
            # every caller checks it; these five discarded it, so a worker whose lease had
            # already gone kept running the pipeline -- LLM calls included -- until the
            # terminal write finally refused.
            #
            # Reported, NOT acted on, because False is ambiguous: episode_stamping returns
            # it for lost ownership, for a missing node, AND for a call with no fields to
            # set. Treating it as proof of ownership loss would repeat CF-205 exactly.
            logger.warning(
                "Episode progress stamp did not apply episode_id=%s worker=%s; "
                "the episode is no longer owned by this worker or no longer exists",
                ctx.episode_uuid,
                ctx.worker_id,
            )
        for node_uuid, freshness in freshness_map.items():
            if freshness != FreshnessState.COMPRESSED:
                continue
            rehydrate_started = perf_counter()
            rehydrated = await ctx.lifecycle_service.rehydrate_node(
                node_uuid,
                new_context=episode_content,
                source_node_uuid=node_uuid,
                source_episode_uuid=resolved_episode_uuid,
            )
            rehydrate_duration_ms = int((perf_counter() - rehydrate_started) * 1000)
            record_mcp_event(
                kind="background",
                operation="rehydration",
                payload={
                    "node_uuid": node_uuid,
                    "source_node_uuid": node_uuid,
                    "source_episode_uuid": resolved_episode_uuid,
                    "trigger": "ingestion",
                },
                result={"rehydrated": rehydrated},
                duration_ms=rehydrate_duration_ms,
                success=rehydrated,
            )
    stamped_ok = ctx.graph_adapter.update_episode_processing(
        ctx.episode_uuid,
        worker_id=ctx.worker_id,
        stage="finalizing",
        substage="marking_ready",
        progress=95.0,
        steps_total=ctx.processing_steps_total,
        steps_completed=5,
        clear_llm_active=True,
    )
    if not stamped_ok:
        # CF-233: the stamp did not apply. The sibling terminal writes
        # (mark_episode_ready/mark_episode_failed) have the identical bool contract and
        # every caller checks it; these five discarded it, so a worker whose lease had
        # already gone kept running the pipeline -- LLM calls included -- until the
        # terminal write finally refused.
        #
        # Reported, NOT acted on, because False is ambiguous: episode_stamping returns
        # it for lost ownership, for a missing node, AND for a call with no fields to
        # set. Treating it as proof of ownership loss would repeat CF-205 exactly.
        logger.warning(
            "Episode progress stamp did not apply episode_id=%s worker=%s; "
            "the episode is no longer owned by this worker or no longer exists",
            ctx.episode_uuid,
            ctx.worker_id,
        )
    marked_ready = ctx.graph_adapter.mark_episode_ready(
        ctx.episode_uuid,
        worker_id=ctx.worker_id,
        resolved_episode_uuid=resolved_episode_uuid,
        nodes_touched=stamped.nodes_touched,
        edges_touched=stamped.edges_touched,
    )
    if not marked_ready:
        logger.info(
            "Skipping ready finalization after ownership lost episode_id=%s worker=%s",
            ctx.episode_uuid,
            ctx.worker_id,
        )
        return
    record_lifecycle_event(
        component="ingest_worker",
        event="episode_ready",
        state="completed",
        episode_uuid=ctx.episode_uuid,
        details={
            "resolved_episode_uuid": resolved_episode_uuid,
            "nodes_touched": stamped.nodes_touched,
            "edges_touched": stamped.edges_touched,
        },
    )
    await emit_scheduler_task_event(
        parent_job_id=ctx.episode_uuid,
        parent_label=str(ctx.claimed.get("name") or ctx.episode_uuid),
        parent_state="ready",
        parent_heartbeat_at=datetime.now(timezone.utc).isoformat(),
        parent_metadata=build_episode_parent_metadata(
            attempts=ctx.processing_attempts,
            source=str(ctx.claimed.get("source") or ""),
            content=str(ctx.claimed.get("content") or ""),
            name=str(ctx.claimed.get("name") or ctx.episode_uuid),
        ),
    )
    duration_ms = int((perf_counter() - ctx.started) * 1000)
    record_mcp_event(
        kind="background",
        operation="episode_enrichment",
        payload={
            "episode_uuid": ctx.episode_uuid,
            "processing_attempts": ctx.processing_attempts,
            "queue_depth": ctx.get_queue_depth(),
        },
        result={
            "resolved_episode_uuid": resolved_episode_uuid,
            "nodes_touched": stamped.nodes_touched,
            "edges_touched": stamped.edges_touched,
        },
        duration_ms=duration_ms,
        success=True,
    )
    if duration_ms > ctx.ready_warning_ms:
        logger.warning(
            "Slow background enrichment episode_id=%s duration_ms=%s queue_depth=%s",
            ctx.episode_uuid,
            duration_ms,
            ctx.get_queue_depth(),
        )


# ---------------------------------------------------------------------------
# Pipeline step 5 — handle enrichment failure
# ---------------------------------------------------------------------------

async def handle_enrichment_failure(
    ctx: EnrichmentContext,
    exc: BaseException,
) -> None:
    """Classify and emit events for enrichment failures.

    Note: the caller is responsible for incrementing ``_failed_enrichments``
    *before* calling this function.
    """

    duration_ms = int((perf_counter() - ctx.started) * 1000)
    failed = ctx.graph_adapter.mark_episode_failed(
        ctx.episode_uuid,
        str(exc),
        worker_id=ctx.worker_id,
    )
    if not failed:
        logger.info(
            "Skipping failed finalization after ownership lost episode_id=%s worker=%s error=%s",
            ctx.episode_uuid,
            ctx.worker_id,
            exc,
        )
        return
    error_type = type(exc).__name__
    classification = classify_enrichment_failure(exc, error_type=error_type)
    failure_stage = (
        "graphiti_invalid_output"
        if is_graphiti_output_parse_error(exc, error_type=error_type)
        else "graphiti_exception"
    )
    failure_details = {
        "source": ctx.claimed.get("source"),
        "session_id": ctx.claimed.get("session_id"),
        "user_id": ctx.claimed.get("user_id"),
        "duration_ms": duration_ms,
    }
    failure_details.update(failure_details_from_exception(exc))
    await emit_scheduler_task_event(
        parent_job_id=ctx.episode_uuid,
        parent_label=str(ctx.claimed.get("name") or ctx.episode_uuid),
        parent_state="failed",
        parent_heartbeat_at=datetime.now(timezone.utc).isoformat(),
        parent_error=str(exc),
        parent_metadata=build_episode_parent_metadata(
            attempts=ctx.processing_attempts,
            source=str(ctx.claimed.get("source") or ""),
            content=str(ctx.claimed.get("content") or ""),
            name=str(ctx.claimed.get("name") or ctx.episode_uuid),
        ),
    )
    record_lifecycle_event(
        component="ingest_worker",
        event="episode_failed",
        state="failed",
        episode_uuid=ctx.episode_uuid,
        details={"error_type": type(exc).__name__, "error": str(exc)},
    )
    record_mcp_event(
        kind="background",
        operation="episode_enrichment",
        payload={
            "episode_uuid": ctx.episode_uuid,
            "processing_attempts": ctx.processing_attempts,
            "queue_depth": ctx.get_queue_depth(),
        },
        duration_ms=duration_ms,
        success=False,
        error=str(exc),
    )
    record_failure_event(
        operation="episode_enrichment",
        episode_uuid=ctx.episode_uuid,
        failure_stage=failure_stage,
        classification=classification,
        retryable=classification == "retryable",
        processing_attempt=ctx.processing_attempts,
        queue_depth=ctx.get_queue_depth(),
        worker_id=ctx.worker_id,
        error_type=error_type,
        error=str(exc),
        traceback_text="".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        ),
        details=failure_details,
    )
    logger.exception("Background enrichment failed episode_id=%s", ctx.episode_uuid)


# ---------------------------------------------------------------------------
# Dual-path helper — timeout-bounded Graphiti add_episode
# ---------------------------------------------------------------------------

async def add_episode_with_timeout(
    graphiti_client: GraphitiClient,
    *,
    name: str,
    episode_body: str,
    source_description: str,
    reference_time: datetime,
    episode_uuid: str | None = None,
    attempt: int = 1,
    timeout_s: float = 300.0,
    group_id: str = "",
    relationless_repair_context_loader: Callable[[], tuple[str, ...]] | None = None,
    self_identity: SelfIdentityContext | None = None,
    self_subject_endpoint: SelfSubjectEndpointEnvelope | None = None,
    self_bind_mode: SelfBindMode = SelfBindMode.OFF,
    self_assertion_authorizer: Any | None = None,
) -> Any:
    """Bound one Graphiti add_episode call so stuck requests fail back into retry flow.

    Takes explicit params (not ctx) because the legacy ``ingest_episode()``
    path also calls this function.

    ``self_identity`` carries the LOGICAL namespace and the trusted author evidence, separately
    from ``group_id``, which is the physical Graphiti partition. Logical ``default`` maps to
    physical ``""``, so identity must never be inferred from ``group_id``. Callers that cannot
    prove an author omit it and no self binding occurs.
    """

    receipt_episode_key = str(episode_uuid or "").strip()
    if self_subject_endpoint is not None:
        if self_bind_mode is not SelfBindMode.ENFORCE:
            raise InvalidSelfSubjectDeclarationError(
                "a self-subject endpoint may be dispatched only in enforce mode"
            )
        if not receipt_episode_key or self_subject_endpoint.episode_uuid != receipt_episode_key:
            raise InvalidSelfSubjectDeclarationError(
                "self-subject endpoint does not belong to the active pending episode"
            )
        if self_identity is None or self_subject_endpoint.namespace != self_identity.namespace:
            raise InvalidSelfSubjectDeclarationError(
                "self-subject endpoint namespace does not match its identity context"
            )
        if self_subject_endpoint.turn_evidence_uuid != str(
            self_identity.turn_evidence_uuid or ""
        ).strip():
            raise InvalidSelfSubjectDeclarationError(
                "self-subject endpoint turn does not match its identity context"
            )
    if (
        self_identity is not None
        and self_identity.evidence_kind is SelfEvidenceKind.EXPLICIT_SELF_SUBJECT
    ):
        # ``name`` is a display/reconciliation anchor, not an episode identifier. Allowing it to
        # stand in here lets a declaration scoped to one pending episode authorize an unrelated
        # Graphiti request that happens to reuse the same name. A structured declaration therefore
        # requires the external pending-episode UUID that owns this exact extraction invocation.
        if not receipt_episode_key:
            raise InvalidSelfSubjectDeclarationError(
                "an exact self-subject declaration requires the pending episode UUID; "
                "the episode name is not identity scope"
            )
        if str(self_identity.episode_uuid or "").strip() != receipt_episode_key:
            raise InvalidSelfSubjectDeclarationError(
                f"declared self subject belongs to episode {self_identity.episode_uuid!r}, not "
                f"pending episode {episode_uuid!r}; refusing Graphiti dispatch"
            )

    record_lifecycle_event(
        component="ingest_worker",
        event="entered_add_episode_timeout_wrapper",
        state="started",
        episode_uuid=episode_uuid,
        details={
            "name": name,
            "timeout_s": timeout_s,
        },
    )
    # Activate the combined-extraction receipt in THIS (parent) task, BEFORE the
    # asyncio.wait_for below. wait_for schedules graphiti_client.add_episode as a
    # separate Task with its own COPIED context, so a receipt created inside that call
    # (or the nested Graphiti add_episode task) would never be visible to the parent
    # task that later runs stamp_and_finalize. Setting the mutable receipt here means
    # both the wait_for child and Graphiti's own child task inherit the same object and
    # mutate it in place, which the parent then reads. (episode_body is the current-
    # episode text used to gate endpoint synthesis.)
    begin_extraction_receipt(
        receipt_episode_key or name,
        episode_body,
        source_description=source_description,
        relationless_repair_context_loader=relationless_repair_context_loader,
        self_identity=self_identity,
        self_subject_endpoint=self_subject_endpoint,
        self_bind_mode=self_bind_mode,
        self_assertion_authorizer=self_assertion_authorizer,
    )
    try:
        record_lifecycle_event(
            component="ingest_worker",
            event="add_episode_timeout_wrapper",
            state="started",
            episode_uuid=episode_uuid,
            details={
                "name": name,
                "timeout_s": timeout_s,
            },
        )
        result = await asyncio.wait_for(
            graphiti_client.add_episode(
                name=name,
                episode_body=episode_body,
                source_description=source_description,
                reference_time=reference_time,
                episode_uuid=episode_uuid,
                attempt=attempt,
                group_id=group_id,
            ),
            timeout=timeout_s,
        )
    except asyncio.TimeoutError as exc:
        # stamp_and_finalize will not run for this episode; drop the receipt so a reused
        # worker task cannot carry stale extraction state into the next episode.
        clear_extraction_receipt()
        record_lifecycle_event(
            component="ingest_worker",
            event="add_episode_timeout_wrapper",
            state="timeout",
            episode_uuid=episode_uuid,
            details={
                "name": name,
                "timeout_s": timeout_s,
            },
        )
        raise TimeoutError(
            "graphiti add_episode timed out after "
            f"{timeout_s:.1f}s; remote completion status unknown"
        ) from exc
    except Exception:  # re-raised; record telemetry for any graphiti failure
        clear_extraction_receipt()
        record_lifecycle_event(
            component="ingest_worker",
            event="add_episode_timeout_wrapper",
            state="failed",
            episode_uuid=episode_uuid,
            details={
                "name": name,
                "timeout_s": timeout_s,
            },
        )
        raise
    else:
        record_lifecycle_event(
            component="ingest_worker",
            event="add_episode_timeout_wrapper",
            state="completed",
            episode_uuid=episode_uuid,
            details={
                "name": name,
                "timeout_s": timeout_s,
            },
        )
        return result
