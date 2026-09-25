"""Background rehydration lifecycle for the recall coordinator mixin stack."""

from __future__ import annotations

import asyncio
import logging
from time import perf_counter

from menhir.infrastructure.telemetry import record_mcp_event

logger = logging.getLogger(__name__)


class RecallSupportRehydrationMixin:
    async def shutdown(self) -> None:
        """Cancel and await all pending background rehydration tasks."""

        tasks = list(self._rehydration_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._rehydration_tasks.clear()

    def _track_rehydration_task(self, task: asyncio.Task[None]) -> None:
        """Retain and cleanup background rehydration tasks."""
        self._rehydration_tasks.add(task)
        task.add_done_callback(self._rehydration_tasks.discard)

    def _schedule_rehydration(self, node_uuid: str, *, source_node_uuid: str | None = None) -> None:
        """Start supervised fire-and-forget rehydration."""
        task = asyncio.create_task(
            self._fire_rehydration(node_uuid, source_node_uuid=source_node_uuid)
        )
        self._track_rehydration_task(task)

    async def _fire_rehydration(self, node_uuid: str, *, source_node_uuid: str | None = None) -> None:
        """Supervised background rehydration for a COMPRESSED node."""
        if self.lifecycle_service is None:
            return

        started = perf_counter()
        try:
            rehydrated = await self.lifecycle_service.rehydrate_node(
                node_uuid,
                new_context=None,
                source_node_uuid=source_node_uuid,
            )
            duration_ms = int((perf_counter() - started) * 1000)
            record_mcp_event(
                kind="background",
                operation="rehydration",
                payload={
                    "node_uuid": node_uuid,
                    "source_node_uuid": source_node_uuid,
                    "trigger": "retrieval",
                },
                result={"rehydrated": rehydrated},
                duration_ms=duration_ms,
                success=rehydrated,
            )
        except Exception:  # background task: log and record, never propagate
            duration_ms = int((perf_counter() - started) * 1000)
            logger.exception("Rehydration failed for node=%s", node_uuid)
            record_mcp_event(
                kind="background",
                operation="rehydration",
                payload={
                    "node_uuid": node_uuid,
                    "source_node_uuid": source_node_uuid,
                    "trigger": "retrieval",
                },
                result={"error": "rehydration_failed"},
                duration_ms=duration_ms,
                success=False,
            )
