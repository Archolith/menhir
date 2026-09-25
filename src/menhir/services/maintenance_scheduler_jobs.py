"""Job coroutine factories and legacy per-job delegators for :class:`MaintenanceScheduler`.

Split out of ``menhir.services.maintenance_scheduler`` (file-size refactor): the methods are
bound onto ``MaintenanceScheduler`` there via this mixin, so the class surface and behavior
are unchanged.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Awaitable

from menhir.services.scheduler_tasks import (
    auto_resolve_conflicts,
    compute_failed_retry_delay_s,
    confirm_conflicts,
    observe_queue_health,
    prune_telemetry_revisions,
    prune_telemetry_tables,
    recover_stale_leases,
    refresh_structure_graphs,
    retry_failed_enrichments,
    review_unresolved_conflicts,
    sync_experience_counters,
    sync_verifiers_job,
)

if TYPE_CHECKING:
    from menhir.services.maintenance_scheduler import _JobState


class _MaintenanceJobWiringMixin:
    """Job coroutine factories and legacy delegators for ``MaintenanceScheduler``."""

    def _make_prune_telemetry_tables(self) -> Awaitable[dict[str, object]]:
        return prune_telemetry_tables(
            observability_days=self.telemetry_observability_retention_days,
            diagnostic_days=self.telemetry_diagnostic_retention_days,
        )

    def _make_prune_telemetry_revisions(self) -> Awaitable[dict[str, object]]:
        return prune_telemetry_revisions(retention_days=self.revision_retention_days)

    def _make_recover_stale_leases(self) -> Awaitable[dict[str, object]]:
        return recover_stale_leases(self.ingest_service, recovery_limit=self.recovery_limit)

    def _make_retry_failed_enrichments(self) -> Awaitable[dict[str, object]]:
        return retry_failed_enrichments(
            self.ingest_service, self.graph_adapter, failed_retry_limit=self.failed_retry_limit,
        )

    def _make_observe_queue_health(self) -> Awaitable[dict[str, object]]:
        return observe_queue_health(self.ingest_service, self.graph_adapter)

    def _make_auto_resolve_conflicts(self) -> Awaitable[dict[str, object]]:
        assert self.lifecycle_service is not None
        return auto_resolve_conflicts(
            self.lifecycle_service,
            max_age_days=self.conflict_auto_resolve_max_age_days,
            limit=self.conflict_auto_resolve_limit,
        )

    def _make_confirm_conflicts(self) -> Awaitable[dict[str, object]]:
        assert self.lifecycle_service is not None
        return confirm_conflicts(self.lifecycle_service, limit=self.conflict_confirm_limit)

    def _make_review_unresolved_conflicts(self) -> Awaitable[dict[str, object]]:
        assert self.lifecycle_service is not None
        return review_unresolved_conflicts(
            self.lifecycle_service, limit=self.conflict_review_unresolved_limit,
        )

    def _make_refresh_structure_graphs(self) -> Awaitable[dict[str, object]]:
        return refresh_structure_graphs(self.graph_adapter)

    def _make_sync_experience_counters(self) -> Awaitable[dict[str, object]]:
        return sync_experience_counters(self.graph_adapter, embed=self.experience_embed)

    def _make_sync_verifiers(self) -> Awaitable[dict[str, object]]:
        return sync_verifiers_job(
            self.graph_adapter,
            verifier_repo=self.verifier_repo,
            verifier_context=self.verifier_context,
            embed=self.experience_embed,
        )

    async def _make_consolidate_lifecycle(self) -> dict[str, object]:
        assert self.lifecycle_service is not None
        result = await self.lifecycle_service.recover_orphans()
        return {
            "promoted": result.promoted,
            "deleted": result.deleted,
            "demoted": result.demoted,
            "conflicts_detected": result.conflicts_detected,
            "skipped_pending": result.skipped_pending,
            "orphan_episodes_cleaned": result.orphan_episodes_cleaned,
        }

    async def _make_decay_lifecycle(self) -> dict[str, object]:
        assert self.lifecycle_service is not None
        result = await self.lifecycle_service.apply_decay()
        return {
            "edge_counts_synced": result.edge_counts_synced,
            "sharpness_recalculated": result.sharpness_recalculated,
            "compressed": result.compressed,
            "deleted": result.deleted,
            "edges_bridged": result.edges_bridged,
            "orphan_subgraphs_cleaned": result.orphan_subgraphs_cleaned,
        }

    # ------------------------------------------------------------------
    # Backward-compat delegators (used by tests)
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_timestamp(value: object | None) -> datetime | None:
        from menhir.services.scheduler_tasks import _parse_timestamp
        return _parse_timestamp(value)

    @staticmethod
    def compute_failed_retry_delay_s(processing_attempts: object | None) -> int:
        return compute_failed_retry_delay_s(processing_attempts)

    async def _retry_process_candidate(
        self, row: dict, max_attempts: int, now: datetime,
    ) -> str:
        from menhir.services.scheduler_tasks import retry_process_candidate
        return await retry_process_candidate(
            self.graph_adapter, self.ingest_service, row, max_attempts, now,
        )

    async def _run_recover_stale_leases(self, job: _JobState) -> None:
        await self._run_job(job, "scheduler_recover_stale_leases", self._make_recover_stale_leases())

    async def _run_retry_failed_enrichments(self, job: _JobState) -> None:
        await self._run_job(job, "scheduler_retry_failed_enrichments", self._make_retry_failed_enrichments())

    async def _run_observe_queue_health(self, job: _JobState) -> None:
        await self._run_job(job, "scheduler_queue_health", self._make_observe_queue_health())

    async def _run_auto_resolve_conflicts(self, job: _JobState) -> None:
        await self._run_job(job, "scheduler_auto_resolve_conflicts", self._make_auto_resolve_conflicts())

    async def _run_confirm_conflicts(self, job: _JobState) -> None:
        await self._run_job(job, "scheduler_confirm_conflicts", self._make_confirm_conflicts())

    async def _run_review_unresolved_conflicts(self, job: _JobState) -> None:
        await self._run_job(job, "scheduler_review_unresolved_conflicts", self._make_review_unresolved_conflicts())
