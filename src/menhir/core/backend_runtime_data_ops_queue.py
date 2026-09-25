"""Episode queue operations for the in-process backend adapter.

Extracted from ``backend_runtime_data_ops``: enqueueing a new episode for enrichment and
re-enqueueing a pending one. Reached through ``RuntimeProviderDataOpsMixin``.
"""

from __future__ import annotations

from typing import Any

from menhir.domain.session import new_session

from .backend_shared import _to_jsonable


class RuntimeQueueDataOpsMixin:
    """Episode enqueue operations for the in-process backend adapter."""

    async def queue_episode(
        self,
        text: str,
        *,
        user_id: str,
        session_id: str,
        source: str = "mcp",
        diff: str | None = None,
        flagged: bool = False,
        bootstrap_scope: str | None = None,
        namespace: str | None = None,
        occurred_at: str | None = None,
        turn_evidence_uuid: str | None = None,
    ) -> dict[str, Any]:
        session = new_session(user_id, session_id=session_id)
        queue_kwargs: dict[str, Any] = {
            "diff": diff,
            "flagged": flagged,
            "namespace": namespace,
            "occurred_at": occurred_at,
            "turn_evidence_uuid": turn_evidence_uuid,
        }
        if bootstrap_scope is not None:
            queue_kwargs["bootstrap_scope"] = bootstrap_scope
        result = await self.built.ingest_service.queue_episode_for_enrichment(
            text, session, source, **queue_kwargs
        )
        return _to_jsonable(result)

    async def enqueue_pending_episode(self, episode_uuid: str) -> bool:
        return bool(
            await self.built.ingest_service.enqueue_pending_episode(episode_uuid)
        )
