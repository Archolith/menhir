"""Typed-scalar backfill and repair runner for personal-memory consolidation."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Protocol

logger = logging.getLogger(__name__)


class CountingLlm(Protocol):
    """Callable LLM seam whose invocation count is shared across consolidation phases."""

    calls: int

    def __call__(self, system: str, user: str) -> str: ...


class ScalarConsolidationGraph(Protocol):
    """Graph capabilities used directly by scalar consolidation."""

    def list_scalar_dirty_namespaces(
        self,
        *,
        perceiver_version: str,
        limit: int,
    ) -> list[str]: ...

    def scalar_state_service(self, *, scalar_history_enabled: bool = False) -> Any: ...

    def load_next_scalar_batch(
        self,
        namespace: str,
        *,
        perceiver_version: str,
        limit: int,
    ) -> list[dict[str, Any]]: ...

    def advance_scalar_cursor(
        self,
        namespace: str,
        *,
        cursor_at: str,
        cursor_uuid: str,
        perceiver_version: str,
        at: str,
    ) -> None: ...

    def retire_counters_superseded_by_scalar(self, *, namespace: str) -> int: ...


@dataclass(frozen=True)
class ScalarConsolidationConfig:
    """Scalar-only settings passed through the scheduler facade."""

    perceiver_version: str = "v1"
    batch_size: int = 500
    repair_limit: int = 200
    lineage_limit: int = 5000
    embed_version: str | None = None
    # Drop the free-text attribute name from the k-sample vote and reconcile it modally afterwards.
    # OFF by default; RECALL-affecting when on (more claims clear the gate), not behaviour-neutral.
    reconcile_attribute: bool = False
    # Same defect relocated into the other two identity fields, plus first-person canonicalization.
    # All OFF by default and all RECALL-affecting when on, for the same reason as the attribute.
    reconcile_scope: bool = False
    reconcile_subject: bool = False
    canonical_self: bool = False
    # When True, scalar_history Views are built alongside scalar_state at ingest and repair time.
    scalar_history_enabled: bool = False
    # Observe-only deterministic shadow comparison; never changes scalar decisions or writes.
    deterministic_shadow_enabled: bool = False
    # Opt-in deterministic router; default off preserves the existing LLM route.
    deterministic_router_enabled: bool = False
    deterministic_router_promoted_classes: tuple[str, ...] = ()


async def select_scalar_targets(
    graph_adapter: ScalarConsolidationGraph,
    *,
    namespaces: list[str] | None,
    perceiver_version: str,
    max_namespaces: int,
) -> list[str]:
    """Snapshot scalar-dirty namespaces before the blocking consolidation worker starts."""

    if namespaces is not None:
        return list(namespaces)
    return await asyncio.to_thread(
        graph_adapter.list_scalar_dirty_namespaces,
        perceiver_version=perceiver_version,
        limit=max_namespaces,
    )


def run_scalar_consolidation(
    graph_adapter: ScalarConsolidationGraph,
    *,
    scalar_targets: list[str],
    namespaces: list[str] | None,
    counting_llm: CountingLlm,
    build_episodes: Callable[[list[dict[str, Any]]], list[Any]],
    k: int,
    threshold: float,
    call_budget: int | None,
    embed: Callable[[str], list[float] | None] | None,
    config: ScalarConsolidationConfig,
) -> dict[str, object]:
    """Run the typed-scalar backfill and repair phases with the shared LLM budget."""

    from menhir.infrastructure.typed_assertion_repository import ScalarStateActivationError
    from menhir.services.typed_scalar_perception import (
        ScalarStateNotActivatedError,
        TypedScalarPerceptionService,
    )

    scalar_states_written = 0
    scalar_advisory = 0
    scalar_processed = 0
    scalar_counters_retired = 0
    scalar_batches = 0
    scalar_service = None
    scalar_state_service = None

    try:
        scalar_state_service = graph_adapter.scalar_state_service(
            scalar_history_enabled=config.scalar_history_enabled)
        scalar_service = TypedScalarPerceptionService(
            graph_adapter,
            scalar_state_service,
            perceiver_version=config.perceiver_version,
            embed=embed,
            embed_version=config.embed_version,
            scalar_history_enabled=config.scalar_history_enabled,
            deterministic_shadow_enabled=config.deterministic_shadow_enabled,
            deterministic_router_enabled=config.deterministic_router_enabled,
            deterministic_router_promoted_classes=config.deterministic_router_promoted_classes,
        )
        scalar_service.ensure_activated()
    except (ScalarStateActivationError, ScalarStateNotActivatedError) as exc:
        logger.warning(
            "scalar-state activation refused; typed-scalar shadow path disabled "
            "(namespaces stay scalar-dirty for retry): %s",
            exc,
        )
        scalar_service = None
        scalar_state_service = None

    if scalar_service is not None:
        for namespace in scalar_targets:
            if call_budget is not None and counting_llm.calls >= call_budget:
                break
            namespace_touched = False
            # Truncation-safe pagination advances only through processed episodes. The shared
            # invocation counter keeps counter + scalar work under one scheduler-run budget.
            while True:
                if call_budget is not None and counting_llm.calls >= call_budget:
                    break
                rows = graph_adapter.load_next_scalar_batch(
                    namespace,
                    perceiver_version=config.perceiver_version,
                    limit=config.batch_size,
                )
                if not rows:
                    break
                episodes = build_episodes(rows)
                if episodes:
                    reference_by_uuid = {
                        str(row["uuid"]): (str(row.get("valid_at") or "") or None)
                        for row in rows
                    }
                    perception = scalar_service.perceive_and_persist(
                        episodes,
                        counting_llm,
                        k=k,
                        threshold=threshold,
                        namespace=namespace,
                        episode_reference_time=reference_by_uuid.get,
                        reconcile_attribute=config.reconcile_attribute,
                        reconcile_scope=config.reconcile_scope,
                        reconcile_subject=config.reconcile_subject,
                        canonical_self=config.canonical_self,
                    )
                    scalar_states_written += int(perception.get("bound", 0))
                    scalar_advisory += int(perception.get("advisory", 0))
                scalar_batches += 1
                namespace_touched = True

                # Cursor position is ingestion order (`cursor_at`), never semantic world time.
                # Fail closed on a wholly timeless page so the namespace stays dirty for repair.
                advanceable = [row for row in rows if str(row.get("cursor_at") or "")]
                if not advanceable:
                    logger.warning(
                        "scalar backfill: page for %s has no orderable timestamp; not "
                        "advancing cursor",
                        namespace,
                    )
                    break
                cursor = advanceable[-1]
                graph_adapter.advance_scalar_cursor(
                    namespace,
                    cursor_at=str(cursor["cursor_at"]),
                    cursor_uuid=str(cursor.get("uuid") or ""),
                    perceiver_version=config.perceiver_version,
                    at=datetime.now(timezone.utc).isoformat(),
                )
                if len(rows) < config.batch_size:
                    break

            # Typed scalar follows the counter pass, so duplicate counter Views are retired only
            # after the authoritative scalar surface exists. Reconciliation stays non-fatal.
            try:
                scalar_counters_retired += int(
                    graph_adapter.retire_counters_superseded_by_scalar(namespace=namespace)
                )
            except Exception:  # noqa: BLE001 - reconciliation must not fail consolidation
                logger.exception(
                    "counter/scalar reconciliation failed for %s (non-fatal)",
                    namespace,
                )
            if namespace_touched:
                scalar_processed += 1

        # Pending-binding repair is one fair, globally bounded query. Explicit namespaces form a
        # deduplicated allowlist; no override keeps the global fair queue.
        allow_list = list(dict.fromkeys(namespaces)) if namespaces is not None else None
        allow_set = set(allow_list) if allow_list is not None else None
        binding_repair = scalar_service.repair_pending_bindings(
            namespaces=allow_list,
            limit=config.repair_limit,
        )
        scalar_repaired = int(binding_repair.get("repaired", 0))
        scalar_repair_pending = int(binding_repair.get("still_pending", 0))

        # Finish durable deletion receipts only after an as-of-now rebuild/retirement succeeds.
        projection_repair = scalar_state_service.activate_due_assertions(
            namespaces=allow_list,
            limit=config.repair_limit,
            as_of=datetime.now(timezone.utc),
        )
        scalar_due_claimed = int(projection_repair.get("claimed", 0))
        scalar_delete_repaired = len(projection_repair.get("repaired", []))
        scalar_delete_repair_failed = len(projection_repair.get("failed", []))

        scalar_recon_repaired = 0
        scalar_orphans_repaired = 0
        scalar_orphans_unresolved = 0
        try:
            from menhir.infrastructure.graph_operations import GraphOperationsJournal

            journal = GraphOperationsJournal()
            lineage = journal.list_committed_merges(page_size=config.lineage_limit)
            unmerges = journal.list_committed_unmerges(page_size=config.lineage_limit)
            journal_ok = True
        except Exception:  # noqa: BLE001 - journal failure must not fail the scheduler job
            logger.exception(
                "scalar repair: could not read merge lineage from the journal; skipping "
                "reconciliation + orphan passes this run"
            )
            journal_ok = False

        if journal_ok:
            reconciliation = scalar_state_service.repair_incomplete_reconciliations(
                committed_merges=lineage,
                committed_unmerges=unmerges,
                allowed_namespaces=allow_set,
            )
            scalar_recon_repaired = int(reconciliation.get("repaired", 0))
            orphan_repair = scalar_state_service.repair_orphaned_assertions(
                committed_merges=lineage,
                allowed_namespaces=allow_set,
                limit=config.repair_limit,
            )
            scalar_orphans_repaired = len(orphan_repair.get("repaired", []))
            scalar_orphans_unresolved = len(orphan_repair.get("unresolved", []))
    else:
        scalar_repaired = 0
        scalar_repair_pending = 0
        scalar_recon_repaired = 0
        scalar_orphans_repaired = 0
        scalar_orphans_unresolved = 0
        scalar_delete_repaired = 0
        scalar_delete_repair_failed = 0
        scalar_due_claimed = 0

    return {
        "scalar_namespaces_dirty": len(scalar_targets),
        "scalar_namespaces_processed": scalar_processed,
        "scalar_batches": scalar_batches,
        "scalar_states_written": scalar_states_written,
        "scalar_advisory": scalar_advisory,
        "scalar_counters_retired": scalar_counters_retired,
        "scalar_repaired": scalar_repaired,
        "scalar_repair_pending": scalar_repair_pending,
        "scalar_delete_repaired": scalar_delete_repaired,
        "scalar_delete_repair_failed": scalar_delete_repair_failed,
        "scalar_due_claimed": scalar_due_claimed,
        "scalar_recon_repaired": scalar_recon_repaired,
        "scalar_orphans_repaired": scalar_orphans_repaired,
        "scalar_orphans_unresolved": scalar_orphans_unresolved,
    }


__all__ = [
    "CountingLlm",
    "ScalarConsolidationConfig",
    "ScalarConsolidationGraph",
    "run_scalar_consolidation",
    "select_scalar_targets",
]
