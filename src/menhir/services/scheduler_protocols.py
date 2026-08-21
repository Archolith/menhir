"""Protocol types for service dependencies across the codebase."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from menhir.infrastructure.episode_repository import PolicyStampResult

if TYPE_CHECKING:
    from menhir.infrastructure.project_scanner import ProjectScanResult
    from menhir.services.lifecycle_models import ConsolidationResult, DecayResult


class SchedulerIngestService(Protocol):
    async def recover_stale_enrichment_leases(self, limit: int = 100) -> tuple[int, int]:
        ...

    async def requeue_failed_episode(self, episode_uuid: str) -> bool:
        ...

    def get_queue_depth(self) -> int:
        ...

    def get_failed_enrichment_count(self) -> int:
        ...

    def get_max_enrichment_attempts(self) -> int:
        ...

    def get_context_window_retry_attempts(self) -> int:
        ...


class SchedulerGraphAdapter(Protocol):
    def fetch_memory_overview(self) -> dict[str, object]:
        ...

    def fetch_failed_episode_retry_candidates(self, limit: int = 100) -> list[dict[str, object]]:
        ...

    def fetch_failed_error_signatures(self, limit: int = 25) -> list[dict[str, object]]:
        ...

    def find_completed_episode_artifact(
        self,
        *,
        anchor_uuid: str,
        anchor_name: str,
    ) -> dict[str, object] | None:
        ...

    def stamp_ingest_metadata(
        self,
        *,
        node_uuids: list[str],
        edge_uuids: list[str],
        session_id: str,
        user_id: str,
        source: str,
        source_confidence: float,
        namespace: str = "default",
    ) -> PolicyStampResult:
        ...

    def mark_episode_ready(
        self,
        episode_uuid: str,
        *,
        worker_id: str | None = None,
        required_state: str | None = None,
        resolved_episode_uuid: str,
        nodes_touched: int,
        edges_touched: int,
    ) -> bool:
        ...

    def list_structure_projects(self) -> list[dict[str, str]]:
        ...

    def list_orphan_structure_projects(self) -> list[dict[str, Any]]:
        ...

    def get_scan_fingerprint(self, project_name: str) -> str | None:
        ...

    def write_project_structure(self, scan: "ProjectScanResult", session_id: str, user_id: str) -> dict[str, int]:
        ...


class SchedulerLifecycleService(Protocol):
    def auto_resolve_stale_conflicts(self, *, max_age_days: int, limit: int) -> int:
        ...

    async def confirm_pending_conflicts(self, *, limit: int, verbose: bool = False, status: str = "pending_llm_review") -> dict:
        ...

    async def recover_orphans(self, max_age_hours: float = 4.0) -> "ConsolidationResult":
        ...

    async def apply_decay(self) -> "DecayResult":
        ...


class LifecycleServiceProtocol(Protocol):
    """Protocol for lifecycle_service as used by IngestService, RecallService, and EnrichmentContext."""

    async def rehydrate_node(
        self,
        node_uuid: str,
        new_context: str | None,
        source_node_uuid: str | None = None,
    ) -> bool:
        ...

    def auto_resolve_stale_conflicts(self, *, max_age_days: int, limit: int) -> int:
        ...

    async def confirm_pending_conflicts(self, *, limit: int, verbose: bool = False, status: str = "pending_llm_review") -> dict[str, Any]:
        ...


class SchedulerLeaseStoreProtocol(Protocol):
    def try_acquire(self, *, lease_name: str, owner_id: str, owner_pid: int, lease_duration_s: float) -> bool:
        ...

    def renew(self, *, lease_name: str, owner_id: str, owner_pid: int, lease_duration_s: float) -> bool:
        ...

    def release(self, *, lease_name: str, owner_id: str) -> None:
        ...

    def fetch(self, *, lease_name: str) -> dict[str, object] | None:
        ...

    def force_acquire(
        self,
        *,
        lease_name: str,
        owner_id: str,
        owner_pid: int,
        lease_duration_s: float,
    ) -> dict[str, object] | None:
        ...
