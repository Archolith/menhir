"""Protocol types for service dependencies across the codebase."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from menhir.infrastructure.project_scanner import ProjectScanResult
    from menhir.services.lifecycle_models import ConsolidationResult, DecayResult

#: Reserved result key (#1005): a scheduled job that returns ``{JOB_FAILED_KEY: True, ...}`` is
#: recorded as unsuccessful. Opt-in; a job that never sets it is recorded exactly as before.
JOB_FAILED_KEY = "job_failed"


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

    def get_llm_session_window_seconds(self) -> int:
        ...


class SchedulerGraphAdapter(Protocol):
    def fetch_memory_overview(self) -> dict[str, object]:
        ...

    def fetch_failed_episode_retry_candidates(self, limit: int = 100) -> list[dict[str, object]]:
        ...

    def park_failed_episode_retry(self, episode_uuid: str, *, failed_at: str, reason: str) -> bool:
        ...

    def fetch_failed_error_signatures(self, limit: int = 25) -> list[dict[str, object]]:
        ...

    def linked_twin_artifact(self, episode_uuid: str) -> dict[str, object] | None:
        ...

    def list_structure_projects(self) -> list[dict[str, str]]:
        ...

    def list_orphan_structure_projects(self) -> list[dict[str, Any]]:
        ...

    def get_scan_fingerprint(
        self, project_name: str, *, project_id: str | None = None
    ) -> str | None:
        ...

    def begin_structure_scan(self, claim: Any) -> int:
        ...

    def refresh_indexed_binding(
        self, project_name: str, fingerprint: str, commit: str, repository: str,
        dirty: bool, *, claim: Any, scan_generation: int,
    ) -> bool:
        ...

    def write_project_structure(self, scan: "ProjectScanResult", session_id: str, user_id: str) -> dict[str, int]:
        ...

    def list_episodes_missing_content_embedding(
        self,
        namespace: str | None = None,
        limit: int = 100,
        *,
        after_uuid: str | None = None,
        model: str | None = None,
        ready_only: bool = False,
        settled_only: bool = False,
        max_failures: int | None = None,
    ) -> list[dict[str, Any]]:
        ...

    def record_source_embedding_failures(
        self, episode_uuids: list[str], limit: int | None = None,
    ) -> dict[str, int]:
        ...

    def set_episode_content_embedding(
        self, episode_uuid: str, embedding: list[float], model: str, *,
        expected_content: str | None = None,
    ) -> bool:
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

    async def record_conflict_pair(self, uuid: str, other_uuid: str) -> tuple[str, int] | None:
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
