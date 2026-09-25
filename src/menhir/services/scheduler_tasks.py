"""Standalone task functions for the maintenance scheduler.

Each function performs one scheduler job and returns a result dict.
The scheduler orchestrator wraps these with timing, telemetry, and
job-state bookkeeping.

This module is the facade for the ``scheduler_tasks_*`` sibling modules: the retry,
conflict, queue-health, counter-sync, structure-refresh, and telemetry-prune job
families live beside it and are re-exported below, so every existing import site of
``menhir.services.scheduler_tasks`` keeps working unchanged. ``consolidate_personal_memory``
and ``_CallCounter`` remain defined here on purpose: the literal-source plumbing pins
(tests/test_gate_relaxations.py, tests/test_scalar_threshold_setting.py,
tests/test_scheduler_task_boundaries.py) and the ``run_scalar_consolidation`` /
``select_event_targets`` / ``run_event_consolidation`` monkeypatch seams resolve against
this module. ``record_failure_event`` stays imported as the patch target for the retry
family, and ``_PROJECT_INDEX_PATH`` stays defined here as the patch target for the
structure family; both siblings resolve them through this module at call time.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from menhir.infrastructure import consolidation_audit as _audit
from menhir.infrastructure.telemetry import record_failure_event
from menhir.services.enrichment_failures import is_budget_refusal
from menhir.services.event_consolidation import (
    EventConsolidationConfig,
    run_event_consolidation,
    select_event_targets,
)
from menhir.services.scalar_consolidation import (
    ScalarConsolidationConfig,
    run_scalar_consolidation,
    select_scalar_targets,
)
from menhir.services.scheduler_protocols import (
    SchedulerGraphAdapter,
    SchedulerIngestService,
)

# Facade re-exports: moved units stay importable from this module path.
from menhir.services.scheduler_tasks_conflicts import (
    auto_resolve_conflicts,
    confirm_conflicts,
    review_unresolved_conflicts,
)
from menhir.services.scheduler_tasks_counters import (
    sync_experience_counters,
    sync_verifiers_job,
)
from menhir.services.scheduler_tasks_health import (
    _NEVER_RETRIED_CLASSIFICATIONS,
    observe_queue_health,
)
from menhir.services.scheduler_tasks_prune import (
    prune_telemetry_revisions,
    prune_telemetry_tables,
)
from menhir.services.scheduler_tasks_retry import (
    _parse_timestamp,
    compute_failed_retry_delay_s,
    retry_failed_enrichments,
    retry_process_candidate,
)
from menhir.services.scheduler_tasks_structure import (
    _write_project_index,
    refresh_structure_graphs,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Hook integration: project index file (patch anchor -- the writer itself lives in
# scheduler_tasks_structure and resolves this constant through the facade at call time)
# ---------------------------------------------------------------------------

_PROJECT_INDEX_PATH = Path.home() / ".claude" / "hooks" / "project-index.json"


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
