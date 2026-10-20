"""Typed-scalar backfill and repair runner for personal-memory consolidation."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Protocol

logger = logging.getLogger(__name__)

_warned_no_scalar_evidence = False


def _warn_once_if_no_scalar_evidence(graph_adapter: Any) -> None:
    """One warning per process when the scalar lane runs but the graph holds no user :TurnEvidence
    at all (issue #95: the Episodic `user:`-prefix fallback is gone, so nothing would ever be
    discovered). Never raises."""
    global _warned_no_scalar_evidence
    if _warned_no_scalar_evidence:
        return
    try:
        if not graph_adapter.evidence_exists():
            _warned_no_scalar_evidence = True
            logger.warning(
                "typed-scalar consolidation found no role='user' TurnEvidence in the graph; "
                "it will discover no work until evidence is recorded via "
                "add_memory(user_statement=...) or a TurnEvidence producer hook"
            )
    except Exception:  # noqa: BLE001 - visibility must never fail target selection
        logger.exception("typed-scalar evidence visibility check failed (non-fatal)")


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

    def evidence_exists(self) -> bool: ...

    # #170 D1 prior-version retirement (reversion_repository).
    def scalar_prior_version_rows(
        self, namespace: str, turn_ids: list[str], *, perceiver_rank: int,
    ) -> list[dict[str, Any]]: ...

    def scalar_current_version_spans(
        self, namespace: str, turn_ids: list[str], *, perceiver_version: str,
        include_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]: ...

    def scalar_owed_subjects(
        self, namespace: str, turn_ids: list[str], *, perceiver_rank: int,
    ) -> list[str]: ...

    def retire_scalar_prior_version(
        self, namespace: str, turn_ids: list[str], *, perceiver_rank: int,
        perceiver_version: str, keep_ids: list[str] | None = None,
    ) -> dict[str, Any]: ...

    def mark_projection_complete(self, assertion_ids: list[str]) -> int: ...


@dataclass(frozen=True)
class ScalarConsolidationConfig:
    """Scalar-only settings passed through the scheduler facade."""

    perceiver_version: str = "v1"
    batch_size: int = 500
    repair_limit: int = 200
    lineage_limit: int = 5000
    embed_version: str | None = None
    # Drop the free-text attribute name from the k-sample vote and reconcile it modally afterwards.
    # ON by default (lenient-agreement defaults, 2026-09, matching MemorySettings);
    # RECALL-affecting when on (more claims clear the gate), not behaviour-neutral.
    reconcile_attribute: bool = True
    # Same defect relocated into the other two identity fields, plus first-person canonicalization.
    # Scope/subject ON by default (matching MemorySettings); canonical_self stays OFF.
    reconcile_scope: bool = True
    reconcile_subject: bool = True
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

    _warn_once_if_no_scalar_evidence(graph_adapter)
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

    from menhir.domain.typed_assertion import perceiver_rank
    from menhir.infrastructure.typed_assertion_repository import ScalarStateActivationError
    from menhir.services.typed_scalar_perception import (
        ScalarStateNotActivatedError,
        TypedScalarPerceptionService,
    )

    scalar_states_written = 0  # bound assertions (the key predates the stage split)
    scalar_advisory = 0
    # Stage counts (#170 item 4): rows read, gate decisions, committed, persisted, binding
    # mismatches, Views rebuilt. Bound is scalar_states_written.
    scalar_evidence_rows = 0
    scalar_decisions = 0
    scalar_committed = 0
    scalar_persisted = 0
    scalar_mismatched = 0
    scalar_views_rebuilt = 0
    scalar_processed = 0
    scalar_counters_retired = 0
    scalar_batches = 0
    # #170 D1: prior-version readings kept (rule B) and retired after a re-read.
    scalar_carried = 0
    scalar_retired = 0
    # 0 = no retirement: a non-SemVer version, or a graph without the retire queries (test fakes;
    # MemoryGraphAdapter always has them, pinned by test_prior_version_retire).
    rank = (perceiver_rank(config.perceiver_version)
            if callable(getattr(graph_adapter, "retire_scalar_prior_version", None)) else 0)
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
                scalar_evidence_rows += len(rows)
                episodes = build_episodes(rows)
                reversion_owed: set[str] = set()
                if episodes:
                    reference_by_uuid = {
                        str(row["uuid"]): (str(row.get("valid_at") or "") or None)
                        for row in rows
                    }
                    turn_ids = [str(getattr(e, "uuid", "") or "") for e in episodes]
                    # #170 D1, before persist: the older readings a re-read may replace, and the
                    # subjects an earlier retire at this version left unrebuilt (a crash).
                    prior = (graph_adapter.scalar_prior_version_rows(
                        namespace, turn_ids, perceiver_rank=rank) if rank > 0 else [])
                    owed = (set(graph_adapter.scalar_owed_subjects(
                        namespace, turn_ids, perceiver_rank=rank)) if rank > 0 else set())
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
                        # Rebuild once after the retire, so no View holds old and new together.
                        **({"defer_rebuild": True} if prior else {}),
                    )
                    scalar_states_written += int(perception.get("bound", 0))
                    scalar_advisory += int(perception.get("advisory", 0))
                    scalar_decisions += int(perception.get("decisions", 0) or 0)
                    scalar_committed += int(perception.get("committed", 0) or 0)
                    scalar_persisted += int(perception.get("persisted", 0) or 0)
                    scalar_mismatched += int(perception.get("mismatched", 0) or 0)
                    scalar_views_rebuilt += int(perception.get("rebuilt", 0) or 0)
                    if rank > 0:
                        reversion = _retire_prior_scalar_version(
                            graph_adapter, scalar_state_service, namespace, turn_ids,
                            rank=rank, config=config, prior=prior, owed=owed,
                            perception=perception if prior else None)
                        scalar_carried += reversion["carried"]
                        scalar_retired += reversion["retired"]
                        scalar_views_rebuilt += reversion["rebuilt"]
                        reversion_owed = reversion["owed"]
                scalar_batches += 1
                namespace_touched = True
                if reversion_owed:
                    # A retired reading's View did not rebuild. Keep the page unread, so the next
                    # run rebuilds it (scalar_owed_subjects) before the cursor passes these turns.
                    logger.warning(
                        "scalar re-version: %d subject(s) in %s did not rebuild after retire; "
                        "not advancing cursor", len(reversion_owed), namespace)
                    break

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
        "scalar_evidence_rows": scalar_evidence_rows,
        "scalar_decisions": scalar_decisions,
        "scalar_assertions_committed": scalar_committed,
        "scalar_assertions_persisted": scalar_persisted,
        "scalar_binding_mismatched": scalar_mismatched,
        "scalar_views_rebuilt": scalar_views_rebuilt,
        "scalar_counters_retired": scalar_counters_retired,
        "scalar_prior_version_carried": scalar_carried,
        "scalar_prior_version_retired": scalar_retired,
        "scalar_repaired": scalar_repaired,
        "scalar_repair_pending": scalar_repair_pending,
        "scalar_delete_repaired": scalar_delete_repaired,
        "scalar_delete_repair_failed": scalar_delete_repair_failed,
        "scalar_due_claimed": scalar_due_claimed,
        "scalar_recon_repaired": scalar_recon_repaired,
        "scalar_orphans_repaired": scalar_orphans_repaired,
        "scalar_orphans_unresolved": scalar_orphans_unresolved,
    }


def _retire_prior_scalar_version(
    graph_adapter: ScalarConsolidationGraph,
    state_service: Any,
    namespace: str,
    turn_ids: list[str],
    *,
    rank: int,
    config: ScalarConsolidationConfig,
    prior: list[dict[str, Any]],
    owed: set[str],
    perception: dict[str, Any] | None,
) -> dict[str, Any]:
    """#170 D1, counting's rule B for the scalar lane, after a page is stored at this version.

    Keeps a prior reading the new samples proposed something over but nothing was stored for (a
    gate veto, or a sample that could not read the turn); retires the rest, and every lower-rank
    reading written on these turns since `prior` was read. Then rebuilds each touched subject once.
    `perception` is the deferred persist result (None when nothing was deferred). Returns
    {carried, retired, rebuilt, owed}; `owed` is the retired subjects whose rebuild failed."""
    from menhir.services.counting_consolidation import _rebuild_page, reversion_carried

    page_rows: list[dict[str, Any]] = []
    spans: dict[str, Any] = {}
    if perception is not None:
        spans = dict(perception.get("spans") or {})
        page_rows = [r for r in perception.get("results") or []
                     if r.get("bound") and r.get("assertion_id")]
    carry: list[str] = []
    if prior:
        stored = graph_adapter.scalar_current_version_spans(
            namespace, turn_ids, perceiver_version=config.perceiver_version,
            include_ids=[str(r["assertion_id"]) for r in page_rows])
        carry = reversion_carried(prior, spans, stored)
    # Also raises the turns' read marker, so a slower older-version writer cannot make a reading
    # current here afterwards (_RECORD_CYPHER).
    retired = graph_adapter.retire_scalar_prior_version(
        namespace, turn_ids, perceiver_rank=rank, perceiver_version=config.perceiver_version,
        keep_ids=carry)
    due = set(retired.get("subjects") or []) | set(owed)
    rebuilt = _rebuild_page(graph_adapter, state_service, namespace, due, page_rows,
                            history=config.scalar_history_enabled)
    return {"carried": len(carry), "retired": int(retired.get("retired") or 0),
            "rebuilt": len(rebuilt), "owed": due - rebuilt - {""}}


__all__ = [
    "CountingLlm",
    "ScalarConsolidationConfig",
    "ScalarConsolidationGraph",
    "run_scalar_consolidation",
    "select_scalar_targets",
]
