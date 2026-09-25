"""Telemetry read and conflict-resolution-record operations for the in-process backend adapter."""

from __future__ import annotations

from typing import Any

from .backend_shared import _to_jsonable


class RuntimeTelemetryOpsMixin:
    """Telemetry read and conflict-resolution-record operations for the in-process backend adapter."""

    async def fetch_operation_stats(
        self, since_hours: int = 24
    ) -> list[dict[str, Any]]:
        return _to_jsonable(
            await self._off_loop(
                self.telemetry.fetch_operation_stats, since_hours=since_hours
            )
        )

    async def fetch_failure_summary(self, since_hours: int = 24) -> dict[str, Any]:
        return _to_jsonable(
            await self._off_loop(
                self.telemetry.fetch_failure_summary, since_hours=since_hours
            )
        )

    async def fetch_enrichment_rate(self, since_hours: int = 24) -> dict[str, Any]:
        return _to_jsonable(
            await self._off_loop(
                self.telemetry.fetch_enrichment_rate, since_hours=since_hours
            )
        )

    async def fetch_lifecycle_summary(self, since_hours: int = 24) -> dict[str, Any]:
        return _to_jsonable(
            await self._off_loop(
                self.telemetry.fetch_lifecycle_summary, since_hours=since_hours
            )
        )

    async def fetch_episode_task_events(
        self, episode_uuid: str, *, limit: int = 50
    ) -> list[dict[str, Any]]:
        return _to_jsonable(
            await self._off_loop(
                self.telemetry.fetch_episode_task_events,
                episode_uuid=episode_uuid,
                limit=limit,
            )
        )

    async def fetch_recent_failures(
        self, *, limit: int = 20, episode_uuid: str | None = None
    ) -> list[dict[str, Any]]:
        return _to_jsonable(
            await self._off_loop(
                self.telemetry.fetch_recent_failures,
                limit=limit,
                episode_uuid=episode_uuid,
            )
        )

    async def fetch_recent_lifecycle_events(
        self, *, limit: int = 20, episode_uuid: str | None = None
    ) -> list[dict[str, Any]]:
        return _to_jsonable(
            await self._off_loop(
                self.telemetry.fetch_recent_lifecycle_events,
                limit=limit,
                episode_uuid=episode_uuid,
            )
        )

    async def record_conflict_resolution(
        self,
        *,
        uuid_a: str,
        uuid_b: str,
        status: str,
        group_id: str,
        action: str,
        reviewed_by: str,
    ) -> None:
        await self._off_loop(
            self.telemetry.record_conflict_resolution,
            uuid_a=uuid_a,
            uuid_b=uuid_b,
            status=status,
            group_id=group_id,
            action=action,
            reviewed_by=reviewed_by,
        )
