"""Standalone task functions for the maintenance scheduler.

Each function performs one scheduler job and returns a result dict.
The scheduler orchestrator wraps these with timing, telemetry, and
job-state bookkeeping.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from menhir.domain.utils import source_confidence_for
from menhir.infrastructure import consolidation_audit as _audit
from menhir.infrastructure.episode_repository import is_recoverable_context_window_error
from menhir.infrastructure.telemetry import record_failure_event
from menhir.services.enrichment_failures import classify_enrichment_failure, is_budget_refusal
from menhir.services.enrichment_steps import propagate_user_flag
from menhir.services.event_consolidation import (
    EventConsolidationConfig,
    run_event_consolidation,
    select_event_targets,
)
from menhir.services.failure_counter_bridge import sync_failure_counters
from menhir.services.instability_counter_bridge import sync_instability_counters
from menhir.services.scalar_consolidation import (
    ScalarConsolidationConfig,
    run_scalar_consolidation,
    select_scalar_targets,
)
from menhir.services.scheduler_protocols import (
    SchedulerGraphAdapter,
    SchedulerIngestService,
    SchedulerLifecycleService,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_timestamp(value: object | None) -> datetime | None:
    if value is None:
        return None
    rendered = str(value).strip()
    if not rendered:
        return None
    if rendered.endswith("Z"):
        rendered = rendered[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(rendered)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def compute_failed_retry_delay_s(processing_attempts: object | None) -> int:
    attempts = max(1, int(processing_attempts or 1))
    return 30 * (2 ** max(0, attempts - 1))


# ---------------------------------------------------------------------------
# Job: recover stale enrichment leases
# ---------------------------------------------------------------------------

async def recover_stale_leases(
    ingest_service: SchedulerIngestService,
    *,
    recovery_limit: int = 100,
) -> dict[str, object]:
    stale_resets, queued = await ingest_service.recover_stale_enrichment_leases(
        limit=recovery_limit,
    )
    return {
        "stale_resets": stale_resets,
        "queued": queued,
        "queue_depth": ingest_service.get_queue_depth(),
    }


# ---------------------------------------------------------------------------
# Job: observe queue health
# ---------------------------------------------------------------------------

#: Classifications that `retry_process_candidate` refuses to requeue. Episodes carrying one
#: are done being handled automatically -- no scheduler pass will ever pick them up again.
_NEVER_RETRIED_CLASSIFICATIONS = ("terminal", "manual_review")


async def observe_queue_health(
    ingest_service: SchedulerIngestService,
    graph_adapter: SchedulerGraphAdapter,
) -> dict[str, object]:
    """Report queue counts, and WARN loudly about episodes nothing will ever retry.

    A FAILED episode classified `terminal` or `manual_review` is parked permanently: it holds
    its content but has no `:Entity` nodes, and recall searches `:Entity`, so the memory is
    invisible while `add_memory` already reported success to the caller. Nothing else in the
    system says so -- `_run_job` files this job's return value into the telemetry sidecar and
    never logs it, so a backlog here is silent by construction.

    That is exactly how 196 episodes sat unrecallable for eight months (an OpenAI 400
    context-length error fell through every marker list to the `manual_review` default, which
    is correct -- a 400 is not retryable -- but nothing surfaced the growing pile). The counts
    below are cheap; the warning is the point.
    """
    # CF-99: the maintenance loop must never block. A blocked loop cannot renew the
    # scheduler lease, which lets a second owner start mutating the same graph.
    overview = await asyncio.to_thread(graph_adapter.fetch_memory_overview)
    result: dict[str, object] = {
        "queue_depth": ingest_service.get_queue_depth(),
        "failed_enrichments": ingest_service.get_failed_enrichment_count(),
        "pending_count": overview.get("pending_count"),
        "enriching_count": overview.get("enriching_count"),
        "failed_count": overview.get("failed_count"),
    }

    try:
        signatures = await asyncio.to_thread(
            graph_adapter.fetch_failed_error_signatures, limit=25
        )
    except Exception as exc:  # noqa: BLE001 - health reporting must never break the loop
        logger.warning("queue health: could not group failed-episode errors: %s", exc)
        return result

    buckets: dict[str, int] = {}
    budget_parked = 0
    worst: tuple[int, str, str] | None = None  # (count, error, oldest_at)
    for row in signatures:
        error = str(row.get("error") or "")
        count = int(row.get("count") or 0)
        classification = classify_enrichment_failure(error)
        buckets[classification] = buckets.get(classification, 0) + count
        if is_budget_refusal(error):
            budget_parked += count
        if classification in _NEVER_RETRIED_CLASSIFICATIONS:
            if worst is None or count > worst[0]:
                worst = (count, error, str(row.get("oldest_at") or "unknown"))

    stuck = sum(buckets.get(name, 0) for name in _NEVER_RETRIED_CLASSIFICATIONS)
    result["failed_by_classification"] = dict(sorted(buckets.items()))
    result["awaiting_manual_review"] = stuck
    # Broken out of the parked pile because the two need OPPOSITE operator actions: a parse-error
    # pile is a model or prompt problem, a budget pile means the cap is too low for real episodes
    # and is fixed by raising it and requeueing. Folded together they read as one backlog with no
    # indicated action -- which is how the 196-episode pile above stayed unactioned.
    result["parked_on_budget"] = budget_parked

    if stuck > 0 and worst is not None:
        top_count, top_error, oldest_at = worst
        logger.warning(
            "STUCK ENRICHMENT BACKLOG: %d failed episode(s) will NEVER be retried "
            "automatically (%s). Their content is in the graph but has no entities, so it is "
            "invisible to recall even though add_memory reported success. Oldest since %s. "
            "Top cause (%d): %.200s. Fix the root cause, then recover with "
            "scripts/retry_failed_episodes.py --apply.",
            stuck,
            ", ".join(f"{k}={v}" for k, v in sorted(buckets.items()) if k in _NEVER_RETRIED_CLASSIFICATIONS),
            oldest_at,
            top_count,
            top_error.replace("\n", " "),
        )

    return result


# ---------------------------------------------------------------------------
# Job: auto-resolve stale conflicts
# ---------------------------------------------------------------------------

async def auto_resolve_conflicts(
    lifecycle_service: SchedulerLifecycleService,
    *,
    max_age_days: int = 14,
    limit: int = 50,
) -> dict[str, object]:
    groups_resolved = await asyncio.to_thread(
        lifecycle_service.auto_resolve_stale_conflicts,
        max_age_days=max_age_days,
        limit=limit,
    )
    return {"groups_resolved": groups_resolved}


# ---------------------------------------------------------------------------
# Job: confirm pending conflicts via LLM
# ---------------------------------------------------------------------------

async def confirm_conflicts(
    lifecycle_service: SchedulerLifecycleService,
    *,
    limit: int = 20,
) -> dict[str, object]:
    counts = await lifecycle_service.confirm_pending_conflicts(limit=limit)
    return dict(counts)


# ---------------------------------------------------------------------------
# Job: LLM-review unresolved conflicts (weekly, clears false positives)
# ---------------------------------------------------------------------------

async def review_unresolved_conflicts(
    lifecycle_service: SchedulerLifecycleService,
    *,
    limit: int = 50,
) -> dict[str, object]:
    """Re-evaluate unresolved conflict groups through the LLM reviewer.

    Groups that the LLM clears as false positives are resolved as keep_both.
    Groups that the LLM confirms as genuine contradictions stay unresolved
    for manual attention. This prevents the unresolved queue from growing
    indefinitely with pairs that are merely similar but not contradictory.
    """
    counts = await lifecycle_service.confirm_pending_conflicts(
        limit=limit, status="unresolved", verbose=True,
    )
    logger.info(
        "review_unresolved_conflicts: confirmed=%s cleared=%s errors=%s",
        counts.get("confirmed"), counts.get("cleared"), counts.get("errors"),
    )
    return dict(counts)


# ---------------------------------------------------------------------------
# Job: retry failed enrichments
# ---------------------------------------------------------------------------

async def retry_process_candidate(
    graph_adapter: SchedulerGraphAdapter,
    ingest_service: SchedulerIngestService,
    row: dict,
    max_attempts: int,
    now: datetime,
) -> str:
    """Decide what to do with one failed enrichment candidate. Returns the action taken."""
    episode_uuid = str(row.get("uuid") or "")
    anchor_name = str(row.get("name") or "")
    processing_attempts = int(row.get("processing_attempts") or 0)

    if episode_uuid and anchor_name:
        existing_completion = await asyncio.to_thread(
            graph_adapter.find_completed_episode_artifact,
            anchor_uuid=episode_uuid,
            anchor_name=anchor_name,
        )
        if existing_completion is not None:
            resolved_episode_uuid = str(existing_completion.get("resolved_episode_uuid") or "")
            entity_uuids = [str(u) for u in (existing_completion.get("entity_uuids") or []) if str(u)]
            edge_uuids = [str(u) for u in (existing_completion.get("edge_uuids") or []) if str(u)]
            stamp_kwargs: dict[str, object] = {}
            if row.get("bootstrap_scope") is not None:
                stamp_kwargs["bootstrap_scope"] = row.get("bootstrap_scope")
            stamped = await asyncio.to_thread(
                graph_adapter.stamp_ingest_metadata,
                node_uuids=[resolved_episode_uuid] + entity_uuids,
                edge_uuids=edge_uuids,
                session_id=str(row.get("session_id") or ""),
                user_id=str(row.get("user_id") or ""),
                source=str(row.get("source") or "claude-code"),
                source_confidence=source_confidence_for(str(row.get("source") or "claude-code")),
                namespace=str(row.get("namespace") or "default"),
                **stamp_kwargs,
            )
            if bool(row.get("user_flagged", False)):
                propagate_user_flag(
                    graph_adapter,
                    entity_uuids,
                    episode_uuid=episode_uuid,
                    bootstrap_scope=row.get("bootstrap_scope"),
                )
            if await asyncio.to_thread(
                graph_adapter.mark_episode_ready,
                episode_uuid,
                required_state="FAILED",
                resolved_episode_uuid=resolved_episode_uuid,
                nodes_touched=stamped.nodes_touched,
                edges_touched=stamped.edges_touched,
            ):
                return "reconciled"

    classification = classify_enrichment_failure(row.get("processing_error"))
    # Only an operator-clearable context-window error (local n_ctx too small) bypasses the
    # terminal gate below and earns the extended retry cap. A hosted provider's hard limit is
    # also a context-window error, but re-sending the same payload cannot ever succeed.
    context_window_mismatch = is_recoverable_context_window_error(row.get("processing_error"))
    error_text = str(row.get("processing_error") or "")
    queue_depth = ingest_service.get_queue_depth()
    context_cap = ingest_service.get_context_window_retry_attempts()
    effective_max = max(max_attempts, context_cap) if context_window_mismatch else max_attempts

    if classification in ("terminal", "manual_review") and not context_window_mismatch:
        record_failure_event(
            operation="scheduler_retry_failed_enrichments",
            episode_uuid=episode_uuid,
            failure_stage="retry_classification",
            classification=classification,
            retryable=False,
            processing_attempt=processing_attempts,
            queue_depth=queue_depth,
            error_type="terminal_failure" if classification == "terminal" else "manual_review_required",
            error=error_text or classification.replace("_", " "),
            details={"decision": "not_requeued"},
        )
        return "terminal"

    if processing_attempts >= effective_max:
        record_failure_event(
            operation="scheduler_retry_failed_enrichments",
            episode_uuid=episode_uuid,
            failure_stage="retry_attempts_exhausted",
            classification="exhausted",
            retryable=False,
            processing_attempt=processing_attempts,
            queue_depth=queue_depth,
            error_type="retry_attempts_exhausted",
            error=error_text or "retry attempts exhausted",
            details={
                "max_attempts": max_attempts,
                "effective_max": effective_max,
                "decision": "not_requeued",
                "context_window_mismatch": context_window_mismatch,
            },
        )
        return "exhausted"

    completed_at = _parse_timestamp(row.get("processing_completed_at"))
    retry_delay_s = compute_failed_retry_delay_s(processing_attempts)
    if completed_at is not None and (now - completed_at).total_seconds() < retry_delay_s:
        record_failure_event(
            operation="scheduler_retry_failed_enrichments",
            episode_uuid=episode_uuid,
            failure_stage="retry_backoff_wait",
            classification="retryable",
            retryable=True,
            processing_attempt=processing_attempts,
            queue_depth=queue_depth,
            error_type="retry_backoff_wait",
            error=error_text or "retry waiting for backoff",
            details={"retry_delay_s": retry_delay_s, "decision": "deferred"},
        )
        return "waiting"

    if await ingest_service.requeue_failed_episode(episode_uuid):
        return "requeued"
    return "skipped"


async def retry_failed_enrichments(
    ingest_service: SchedulerIngestService,
    graph_adapter: SchedulerGraphAdapter,
    *,
    failed_retry_limit: int = 50,
) -> dict[str, object]:
    now = datetime.now(timezone.utc)
    counts: dict[str, int] = dict(reconciled=0, requeued=0, terminal=0, waiting=0, exhausted=0)
    candidates = await asyncio.to_thread(
        graph_adapter.fetch_failed_episode_retry_candidates, limit=failed_retry_limit
    )
    max_attempts = ingest_service.get_max_enrichment_attempts()
    for row in candidates:
        action = await retry_process_candidate(graph_adapter, ingest_service, row, max_attempts, now)
        if action in counts:
            counts[action] += 1
    return {
        "candidates": len(candidates),
        **counts,
        "queue_depth": ingest_service.get_queue_depth(),
    }


# ---------------------------------------------------------------------------
# Job: sync experience counters
# ---------------------------------------------------------------------------

async def sync_experience_counters(
    graph_adapter: SchedulerGraphAdapter,
    *,
    namespace: str = "agent-experience",
    embed: Callable[[str], "list[float] | None"] | None = None,
) -> dict[str, object]:
    """Fold telemetry events into supersedable :Metric instrumentation counters.

    Two deterministic folds over the telemetry store (no LLM): failure_events ->
    '<op>_<err>_failed' counters, and memory_revisions -> '<field>_revised' belief-instability
    counters. Both write via the MetricWriteCoordinator saga as :Metric nodes -- instrumentation,
    excluded from semantic recall by label. Runs on the maintenance loop in prod; disabled in
    benchmark mode with the rest of the scheduler.

    `embed` is accepted for caller compatibility but IGNORED: Metrics do not carry a
    name_embedding (they never rank in recall), so there is nothing to embed. It can be dropped
    once the scheduler stops constructing an experience embedder.
    """
    del embed  # vestigial: Metrics do not embed
    from menhir.infrastructure.telemetry import telemetry_store
    from menhir.infrastructure.graph_operations import GraphOperationsJournal
    from menhir.infrastructure.metric_receipts import MetricReceiptStore
    from menhir.services.metric_write_coordinator import MetricWriteCoordinator

    # The coordinator, journal, and receipts all resolve to the telemetry sidecar DB (the journal
    # and receipts default to default_telemetry_db_path, and the coordinator's db defaults to the
    # journal's), so the saga's PREPARED journal-row + receipt commit atomically in one connection.
    coordinator = MetricWriteCoordinator(
        graph_adapter=graph_adapter,
        journal=GraphOperationsJournal(),
        receipts=MetricReceiptStore(),
        namespace=namespace,
    )

    # sync_failure_counters / sync_instability_counters are synchronous and perform blocking I/O
    # (SQLite fold + Neo4j saga). Run them on a worker thread so the maintenance loop never blocks
    # the asyncio event loop. Mirrors refresh_structure_graphs.
    failures = await asyncio.to_thread(
        sync_failure_counters,
        store=telemetry_store, coordinator=coordinator, namespace=namespace,
    )
    instability = await asyncio.to_thread(
        sync_instability_counters,
        store=telemetry_store, coordinator=coordinator, namespace=namespace,
    )
    return {
        "failure_counters": len(failures),
        "instability_counters": len(instability),
    }


# ---------------------------------------------------------------------------
# Job: sync graph-native verifiers
# ---------------------------------------------------------------------------

async def sync_verifiers_job(
    graph_adapter: SchedulerGraphAdapter,
    *,
    verifier_repo: object,
    verifier_context: object,
    namespace: str = "agent-status",
    embed: Callable[[str], "list[float] | None"] | None = None,
) -> dict[str, object]:
    """Re-derive config/status registers from their source of truth via graph verifiers and flag
    referencing beliefs on change. Blocking (Neo4j reads/writes + embed HTTP) so it runs on a worker
    thread, mirroring sync_experience_counters — the maintenance loop must never block the event loop."""
    from menhir.services.verifier_sync import sync_verifiers

    results = await asyncio.to_thread(
        sync_verifiers,
        repo=verifier_repo, graph_adapter=graph_adapter, context=verifier_context,
        namespace=namespace, embed=embed,
    )
    refreshed = sum(1 for r in results if r.get("status") == "refreshed")
    changed = sum(1 for r in results if r.get("changed"))
    flagged = sum(int(r.get("beliefs_flagged") or 0) for r in results)
    return {
        "verifiers": len(results),
        "refreshed": refreshed,
        "changed": changed,
        "beliefs_flagged": flagged,
    }


# ---------------------------------------------------------------------------
# Job: consolidate personal memory (perception -> counter Views)
# ---------------------------------------------------------------------------

class _CallCounter:
    """Wraps an llm_complete seam to count invocations, so a per-run budget can stop the batch
    between namespaces (resumable: unprocessed dirty namespaces stay dirty for the next run)."""

    def __init__(self, fn: "Callable[[str, str], str]") -> None:
        self._fn = fn
        self.calls = 0

    def __call__(self, system: str, user: str) -> str:
        self.calls += 1
        return self._fn(system, user)


async def consolidate_personal_memory(
    graph_adapter: SchedulerGraphAdapter,
    *,
    llm_complete: "Callable[[str, str], str]",
    embed: Callable[[str], "list[float] | None"] | None = None,
    namespaces: "list[str] | None" = None,
    k: int = 3,
    threshold: float = 1.0,
    call_budget: int | None = None,
    source: str = "perception",
    max_namespaces: int = 200,
    verify_retries: int = 0,
    sum_grounding: bool = False,
    enable_counter_state: bool = True,
    enable_scalar_state: bool = False,
    scalar_state_perceiver_version: str = "v1",
    scalar_reconcile_attribute: bool = False,
    scalar_reconcile_scope: bool = False,
    scalar_reconcile_subject: bool = False,
    scalar_canonical_self: bool = False,
    scalar_threshold: float | None = None,
    scalar_batch_size: int = 500,
    scalar_repair_limit: int = 200,
    scalar_lineage_limit: int = 5000,
    scalar_embed_version: str | None = None,
    scalar_history_enabled: bool = False,
    scalar_deterministic_shadow_enabled: bool = False,
    scalar_deterministic_router_enabled: bool = False,
    scalar_deterministic_router_promoted_classes: tuple[str, ...] = (),
    enable_event_history: bool = False,
    event_history_perceiver_version: str = "v1",
    event_batch_size: int = 500,
) -> dict[str, object]:
    """Re-derive count/amount Views from each DIRTY namespace's user turns via the gated perception
    boundary, pinning ALL bias guards on (cross-check + coref + verify) so an automatic write never
    commits a confident-but-wrong total. Batch re-fold from all episodes each pass (Law-3 correct).
    Blocking (k-sample LLM + Neo4j) so it runs on a worker thread. Budget-capped and resumable:
    a per-namespace watermark debounces both commits and abstains until new user turns arrive."""
    # Ambient correlation id for this pass, so every nested emit (perception, fold, view write,
    # reconcile) replays together. Propagates into the `_run` worker thread via asyncio.to_thread.
    _audit.begin_pass()
    pass_details: dict[str, object] = {
        "source": source, "k": k,
        "namespaces": (list(namespaces) if namespaces is not None else None),
        "scalar_state": enable_scalar_state,
        "scalar_perceiver_version": scalar_state_perceiver_version if enable_scalar_state else None,
        "counter_state": enable_counter_state,
        "scalar_threshold": scalar_threshold if enable_scalar_state else None,
    }
    # Preserve the flag-off audit contract: adding a disabled shadow field would change every
    # consolidation pass payload even when the observe-only feature is not in use.
    if enable_scalar_state and scalar_deterministic_shadow_enabled:
        pass_details["scalar_deterministic_shadow"] = True
    if enable_scalar_state and scalar_deterministic_router_enabled:
        pass_details["scalar_deterministic_router"] = {
            "promoted_classes": sorted(set(scalar_deterministic_router_promoted_classes))[:64],
        }
    if enable_event_history:
        pass_details["event_history"] = True
        pass_details["event_perceiver_version"] = event_history_perceiver_version
    _audit.audit("pass", "start", details=pass_details)
    from menhir.services.perception import Episode, perceive_and_fold

    targets = namespaces if enable_counter_state else []
    if targets is None:
        targets = await asyncio.to_thread(graph_adapter.list_dirty_namespaces, limit=max_namespaces)

    # C.4.3 typed-scalar shadow path selects its OWN dirty set (independent, version-stamped
    # :ScalarConsolidationWatermark cursor), so it never inherits the counter watermark's debounce. An
    # explicit `namespaces` override applies to both paths; otherwise scalar backfills every namespace
    # with episodes beyond its cursor for this perceiver_version.
    scalar_targets: "list[str] | None" = None
    if enable_scalar_state:
        scalar_targets = await select_scalar_targets(
            graph_adapter,
            namespaces=namespaces,
            perceiver_version=scalar_state_perceiver_version,
            max_namespaces=max_namespaces,
        )

    # Event-history backfill selects its OWN dirty set (independent, version-stamped
    # :EventConsolidationWatermark cursor). An explicit `namespaces` override applies to all paths;
    # otherwise event discovery returns the event-dirty namespaces bounded by max_namespaces.
    event_targets: "list[str] | None" = None
    if enable_event_history:
        event_targets = await select_event_targets(
            graph_adapter,
            namespaces=namespaces,
            perceiver_version=event_history_perceiver_version,
            max_namespaces=max_namespaces,
        )

    def _run() -> dict[str, object]:
        from menhir.services.correction_resolver import resolve_corrections
        from menhir.infrastructure.graph_operations import GraphOperationsJournal
        from menhir.infrastructure.metric_receipts import MetricReceiptStore
        from menhir.services.metric_write_coordinator import MetricWriteCoordinator

        # Perception abstention receipts + correction tallies are instrumentation: route them to
        # the :Metric saga so they stay out of the semantic-recall layer.
        run_tally = MetricWriteCoordinator(
            graph_adapter=graph_adapter,
            journal=GraphOperationsJournal(),
            receipts=MetricReceiptStore(),
        )
        counting_llm = _CallCounter(llm_complete)
        views_written = 0
        abstained = 0
        corrections_applied = 0
        processed = 0

        def _build_episodes(rows: list) -> "list[Episode]":
            """Rows -> Episodes, collapsing EXACT duplicate observations.

            A duplicated turn is not free: perception locates each claim by `source_key`
            (episode_uuid + span offsets), so the SAME fact restated in N duplicate episodes becomes
            N competing source claims. Each k-sample quotes whichever copy it happened to read, so
            the votes scatter and no single source_key can reach `threshold=1.0` -- a perfectly
            perceived claim becomes unprovable. Measured on the LME corpus: 37.7% of user episodes
            are exact duplicates, present in all 78 namespaces.

            The key is (body, valid_at) -- the same text at the SAME instant is one observation
            ingested twice. The same text at a DIFFERENT time is a genuine repeated observation and
            is KEPT (90 such rows in that corpus), because collapsing it would erase a real
            restatement. The FIRST occurrence wins: rows arrive oldest-first, so provenance binds to
            the original statement rather than a re-ingested copy.

            Deduping HERE (the rows -> Episode transform) and not in the query is deliberate: the
            caller advances the scalar cursor from `rows`, not from the returned episodes, so a
            dropped duplicate is still passed over. Dropping it in the query would leave it forever
            beyond the cursor and the namespace permanently dirty.

            Scope note: dedup is per CALL, so it collapses duplicates within one page. Duplicates
            split across pages survive; that is bounded by `scalar_batch_size` and acceptable.
            """
            eps: list[Episode] = []
            seen: set[tuple[str, str]] = set()
            for r in rows:
                raw = str(r.get("content") or "")
                # legacy Episodic turns are stored `user: <text>`; raw :Turn evidence has no prefix.
                body = (raw[len("user:"):] if raw[:5].lower() == "user:" else raw).strip()[:2000]
                if len(body) < 8:
                    continue
                stamp = str(r.get("valid_at") or "")
                if (body, stamp) in seen:
                    continue
                seen.add((body, stamp))
                eps.append(Episode(uuid=str(r["uuid"]), content=f"[{stamp[:10]}] {body}"))
            return eps

        for ns in targets:
            if call_budget is not None and counting_llm.calls >= call_budget:
                break
            rows = graph_adapter.load_user_episodes(ns)
            episodes = _build_episodes(rows)
            if episodes:
                res = perceive_and_fold(
                    episodes=episodes, llm_complete=counting_llm, graph_adapter=graph_adapter,
                    k=k, threshold=threshold, namespace=ns, source=source, embed=embed,
                    enable_cross_check=True, enable_coref=True, enable_verify=True,
                    verify_retries=verify_retries,
                    enable_stated_span_guard=True,
                    enable_sum_grounding=sum_grounding,
                    # F1: record per-veto abstention receipts so a miss is observable in prod instead
                    # of silent — silent abstention hid exactly the measure-key scatter this task now
                    # collapses. Routed to the :Metric saga (out of recall) via run_tally.
                    record_abstentions=True,
                    run_tally=run_tally,
                )
                views_written += len(res.committed)
                abstained += len(res.abstained)
                # F2: bind bare numeric corrections ("actually it is 20, not 25") to the unique
                # value-matching current View. Runs AFTER perceive so the View it corrects exists;
                # the correction's later recorded_at wins LWW and survives batch re-folds. No LLM.
                corr = resolve_corrections(
                    rows, graph_adapter, namespace=ns, source=source, embed=embed,
                    run_tally=run_tally)
                corrections_applied += int(corr["corrections_applied"])
            processed += 1
            graph_adapter.mark_consolidated(ns, at=datetime.now(timezone.utc).isoformat())

        result: dict[str, object] = {
            "namespaces_dirty": len(targets),
            "namespaces_processed": processed,
            "views_written": views_written,
            "abstained": abstained,
            "corrections_applied": corrections_applied,
            "llm_calls": counting_llm.calls,
        }

        # Scalar consolidation is independently dirty-tracked but shares this run's counting LLM,
        # so the configured budget remains global across counter and scalar phases.
        if enable_scalar_state:
            result.update(
                run_scalar_consolidation(
                    graph_adapter,
                    scalar_targets=scalar_targets or [],
                    namespaces=namespaces,
                    counting_llm=counting_llm,
                    build_episodes=_build_episodes,
                    k=k,
                    # The scalar pass may run at its OWN agreement threshold: the counter path is
                    # tuned separately and must not move when the scalar gate is relaxed.
                    threshold=(threshold if scalar_threshold is None else scalar_threshold),
                    call_budget=call_budget,
                    embed=embed,
                    config=ScalarConsolidationConfig(
                        perceiver_version=scalar_state_perceiver_version,
                        batch_size=scalar_batch_size,
                        repair_limit=scalar_repair_limit,
                        lineage_limit=scalar_lineage_limit,
                        embed_version=scalar_embed_version,
                        scalar_history_enabled=scalar_history_enabled,
                        deterministic_shadow_enabled=scalar_deterministic_shadow_enabled,
                        deterministic_router_enabled=scalar_deterministic_router_enabled,
                        deterministic_router_promoted_classes=scalar_deterministic_router_promoted_classes,
                        reconcile_attribute=scalar_reconcile_attribute,
                        reconcile_scope=scalar_reconcile_scope,
                        reconcile_subject=scalar_reconcile_subject,
                        canonical_self=scalar_canonical_self,
                    ),
                )
            )
        # Event-history consolidation is independently dirty-tracked but shares this run's counting
        # LLM, so the configured budget remains global across counter, scalar, and event phases.
        if enable_event_history:
            result.update(
                run_event_consolidation(
                    graph_adapter,
                    event_targets=event_targets or [],
                    counting_llm=counting_llm,
                    call_budget=call_budget,
                    config=EventConsolidationConfig(
                        perceiver_version=event_history_perceiver_version,
                        batch_size=event_batch_size,
                    ),
                )
            )
        # `result` is initialized after the counter pass, then the independent scalar pass.
        # Refresh the shared call count here so API callers see the total work actually spent;
        # otherwise a scalar/event-only run reports zero calls even after consuming k samples.
        result["llm_calls"] = counting_llm.calls
        return result

    result = await asyncio.to_thread(_run)
    _audit.audit("pass", "complete", details={
        k: v for k, v in result.items() if isinstance(v, (int, float, str, bool))
    })
    return result


# ---------------------------------------------------------------------------
# Job: refresh structure graphs
# ---------------------------------------------------------------------------

async def refresh_structure_graphs(
    graph_adapter: SchedulerGraphAdapter,
) -> dict[str, object]:
    """Re-scan all known projects whose file fingerprint has changed."""
    from menhir.infrastructure.project_scanner import ProjectScanner

    projects = await asyncio.to_thread(graph_adapter.list_structure_projects)
    if not projects:
        return {"projects_known": 0, "scanned": 0, "skipped": 0, "errors": 0}

    scanner = ProjectScanner()
    scanned = 0
    skipped = 0
    errors = 0
    details: list[dict[str, str]] = []

    for proj in projects:
        name = proj.get("name", "")
        root_path = proj.get("root_path", "")
        if not root_path or not os.path.isdir(root_path):
            errors += 1
            details.append({"project": name, "status": "path_missing"})
            continue

        try:
            scan = await asyncio.to_thread(scanner.scan, root_path, name)
        except Exception as exc:
            errors += 1
            details.append({"project": name, "status": f"scan_error: {exc}"})
            logger.warning("Structure watcher scan failed for %s: %s", name, exc)
            continue

        stored_fp = await asyncio.to_thread(graph_adapter.get_scan_fingerprint, name)
        if stored_fp and stored_fp == scan.scan_fingerprint:
            skipped += 1
            continue

        try:
            # write_project_structure MERGEs thousands of nodes/edges — a heavy synchronous Neo4j
            # write. Offload it (like scanner.scan above) so the maintenance loop never freezes the
            # asyncio event loop while re-writing a large project's structure.
            counts = await asyncio.to_thread(graph_adapter.write_project_structure, scan, "watcher", "system")
            scanned += 1
            details.append({
                "project": name,
                "status": "refreshed",
                "entities": str(counts.get("entities", 0)),
                "edges": str(counts.get("edges", 0)),
            })
        except Exception as exc:
            errors += 1
            details.append({"project": name, "status": f"write_error: {exc}"})
            logger.warning("Structure watcher write failed for %s: %s", name, exc)

    # Write project index for hooks integration
    _write_project_index(projects)

    return {
        "projects_known": len(projects),
        "scanned": scanned,
        "skipped": skipped,
        "errors": errors,
        "details": details,
    }


# ---------------------------------------------------------------------------
# Hook integration: project index file
# ---------------------------------------------------------------------------

_PROJECT_INDEX_PATH = Path.home() / ".claude" / "hooks" / "project-index.json"


def _write_project_index(
    projects: list[dict[str, str]],
    *,
    index_path: Path | None = None,
) -> None:
    """Write a JSON index of ingested projects for hook scripts to read.

    Best-effort — failure is logged but never propagated.
    """
    target = index_path or _PROJECT_INDEX_PATH
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        index = [
            {"name": p.get("name", ""), "root_path": p.get("root_path", "")}
            for p in projects
            if p.get("name") and p.get("root_path")
        ]
        target.write_text(
            json.dumps(index, indent=2),
            encoding="utf-8",
        )
    except Exception:
        logger.debug("Failed to write project index for hooks", exc_info=True)


async def prune_telemetry_tables(
    *, observability_days: int, diagnostic_days: int
) -> dict[str, object]:
    """Delete sidecar telemetry rows past their retention window (CF-171).

    Nothing deleted from any of the nine high-volume tables. The one pruner that existed targeted
    `memory_revisions` -- by write volume the LEAST affected table -- and was itself never wired
    until CF-166. Measured growth is ~664 bytes/row against ~30 rows per ingest, and this is the
    file six writers contend on, so the size is not merely disk: a larger file means longer WAL
    checkpoints and deeper B-trees, which lengthens exactly the CF-170 writes that block the loop.

    A tier set to 0 is skipped entirely, so an operator can disable one window without disabling
    the other. Threaded off the event loop for the same reason as its sibling.
    """
    from menhir.infrastructure.telemetry import telemetry_store

    if observability_days <= 0 and diagnostic_days <= 0:
        return {"pruned": {}, "skipped": "both tiers disabled"}

    deleted = await asyncio.to_thread(
        telemetry_store.prune_telemetry_tables,
        observability_days=observability_days if observability_days > 0 else 10**6,
        diagnostic_days=diagnostic_days if diagnostic_days > 0 else 10**6,
    )
    total = sum(deleted.values())
    if total:
        logger.info(
            "Pruned %d telemetry row(s) across %d table(s): %s",
            total,
            len(deleted),
            ", ".join(f"{t}={n}" for t, n in sorted(deleted.items())),
        )
    return {"pruned": deleted, "total": total}


async def prune_telemetry_revisions(*, retention_days: int) -> dict[str, object]:
    """Delete `memory_revisions` rows past the retention window.

    This job exists because the control it drives did not. `prune_old_revisions` was written,
    fully unit-tested, and never called from production: `grep` found its definition and six
    test references, and nothing else. The setting that is supposed to configure it,
    `MENHIR_REVISION_RETENTION_DAYS`, was parsed into `revision_retention_days` and then read
    nowhere. Meanwhile the operator runbook stated, as fact, that the window was enforced and
    configurable. Three independent failures of one control, and a document asserting it worked.

    Threaded off the event loop: the store does synchronous SQLite with a commit, and the
    sidecar is shared by seven writers.
    """
    from menhir.infrastructure.telemetry import telemetry_store

    days = max(1, int(retention_days))
    deleted = await asyncio.to_thread(
        telemetry_store.prune_old_revisions, retention_days=days
    )
    if deleted:
        logger.info(
            "Pruned %d memory_revisions row(s) older than %d day(s)", deleted, days
        )
    return {"retention_days": days, "rows_deleted": int(deleted)}
