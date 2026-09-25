"""Pipeline and observability operation methods for the HTTP-backed backend adapter.

Extracted from ``backend_client_ops``: episode processing and lease recovery, queue
depths, scheduler control, stats/failure summaries, and system snapshots. Reached through
``BackendClientOpsMixin``, which composes this mixin.
"""

from __future__ import annotations

from typing import Any


class BackendClientPipelineOpsMixin:
    """Episode pipeline, scheduler, and observability operations for the HTTP-backed backend adapter."""

    async def fetch_episode_processing(
        self, episode_uuid: str
    ) -> dict[str, Any] | None:
        return await self._request(
            "fetch_episode_processing", {"episode_uuid": episode_uuid}
        )

    async def list_episode_processing(
        self, *, states: list[str] | None = None, limit: int = 25,
        namespace: str | None = None,
    ) -> list[dict[str, Any]]:
        return await self._request(
            "list_episode_processing",
            {"states": states, "limit": limit, "namespace": namespace},
        )

    async def get_queue_depth(self) -> int:
        return int(await self._request("get_queue_depth"))

    async def get_failed_enrichment_count(self) -> int:
        return int(await self._request("get_failed_enrichment_count"))

    async def force_reset_failed_episode(self, episode_uuid: str) -> bool:
        return bool(
            await self._request(
                "force_reset_failed_episode", {"episode_uuid": episode_uuid}
            )
        )

    async def force_release_episode_lease(
        self, episode_uuid: str, *, max_attempts: int | None = None
    ) -> bool:
        return bool(
            await self._request(
                "force_release_episode_lease",
                {"episode_uuid": episode_uuid, "max_attempts": max_attempts},
            )
        )

    async def fetch_stale_enriching_episodes(
        self, limit: int = 25
    ) -> list[dict[str, Any]]:
        return await self._request("fetch_stale_enriching_episodes", {"limit": limit})

    async def recover_stale_enrichment_leases(self, limit: int = 10) -> dict[str, int]:
        return await self._request("recover_stale_enrichment_leases", {"limit": limit})

    async def get_max_enrichment_attempts(self) -> int:
        return int(await self._request("get_max_enrichment_attempts"))

    async def recover_orphans(self, *, max_age_hours: float = 24.0) -> dict[str, Any]:
        return await self._request("recover_orphans", {"max_age_hours": max_age_hours})

    async def fetch_session_entities(
        self, *, session_id: str | None = None, max_age_hours: float = 24.0
    ) -> list[dict[str, Any]]:
        return await self._request(
            "fetch_session_entities",
            {"session_id": session_id, "max_age_hours": max_age_hours},
        )

    async def scheduler_force_takeover(self, reason: str) -> bool:
        return bool(await self._request("scheduler_force_takeover", {"reason": reason}))

    async def scheduler_status_snapshot(self) -> dict[str, Any] | None:
        return await self._request("scheduler_status_snapshot")

    async def scheduler_pause(self) -> bool:
        return bool(await self._request("scheduler_pause"))

    async def scheduler_resume(self) -> bool:
        return bool(await self._request("scheduler_resume"))

    async def fetch_operation_stats(
        self, since_hours: int = 24
    ) -> list[dict[str, Any]]:
        return await self._request(
            "fetch_operation_stats", {"since_hours": since_hours}
        )

    async def fetch_failure_summary(self, since_hours: int = 24) -> dict[str, Any]:
        return await self._request(
            "fetch_failure_summary", {"since_hours": since_hours}
        )

    async def fetch_enrichment_rate(self, since_hours: int = 24) -> dict[str, Any]:
        return await self._request(
            "fetch_enrichment_rate", {"since_hours": since_hours}
        )

    async def fetch_lifecycle_summary(self, since_hours: int = 24) -> dict[str, Any]:
        return await self._request(
            "fetch_lifecycle_summary", {"since_hours": since_hours}
        )

    async def fetch_episode_task_events(
        self, episode_uuid: str, *, limit: int = 50
    ) -> list[dict[str, Any]]:
        return await self._request(
            "fetch_episode_task_events", {"episode_uuid": episode_uuid, "limit": limit}
        )

    async def fetch_recent_failures(
        self, *, limit: int = 20, episode_uuid: str | None = None
    ) -> list[dict[str, Any]]:
        return await self._request(
            "fetch_recent_failures", {"limit": limit, "episode_uuid": episode_uuid}
        )

    async def fetch_recent_lifecycle_events(
        self, *, limit: int = 20, episode_uuid: str | None = None
    ) -> list[dict[str, Any]]:
        return await self._request(
            "fetch_recent_lifecycle_events",
            {"limit": limit, "episode_uuid": episode_uuid},
        )

    async def circuit_breaker_snapshots(self) -> dict[str, dict[str, Any]]:
        return await self._request("circuit_breaker_snapshots")

    async def embedding_cache_stats(self) -> dict[str, int]:
        return await self._request("embedding_cache_stats")

    async def get_provider_config(self) -> dict[str, Any]:
        return await self._request("get_provider_config")
