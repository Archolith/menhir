"""MCP tool: force_release_enrichment_lease."""

from __future__ import annotations

from menhir.mcp.formatters import _require_episode_uuid
from menhir.mcp.telemetry import record_lifecycle_event
from menhir.mcp.tools.base import BaseTextTool


async def force_release_enrichment_lease(episode_uuid: str, requeue: bool = True) -> str:
    """Force-release one ENRICHING episode lease, failing exhausted rows instead of requeueing them."""

    return await ForceReleaseEnrichmentLeaseTool().execute(episode_uuid=episode_uuid, requeue=requeue)


class ForceReleaseEnrichmentLeaseTool(BaseTextTool):
    name = "force_release_enrichment_lease"
    required_tier = "operator"
    description = "Force-release one ENRICHING episode lease."

    async def endpoint(self, episode_uuid: str, requeue: bool = True) -> str:
        backend = self.get_backend()
        normalized_uuid = _require_episode_uuid(episode_uuid)

        released = await backend.force_release_episode_lease(
            normalized_uuid,
            max_attempts=await backend.get_max_enrichment_attempts(),
        )
        if not released:
            return (
                f"episode_uuid: {normalized_uuid}\n"
                "result: NOT_FOUND — no ENRICHING episode with that UUID"
            )
        record_lifecycle_event(
            component="operator",
            event="force_release_enrichment_lease",
            state="completed",
            episode_uuid=normalized_uuid,
            details={"requeue": requeue},
        )

        queued = False
        queue_result = "not_requested"
        row = await backend.fetch_episode_processing(normalized_uuid)
        state = str(row.get("processing_state") or "") if row else ""
        if requeue and state == "PENDING":
            queued = await backend.enqueue_pending_episode(normalized_uuid)
            queue_result = "queued" if queued else "already_queued"
        elif requeue:
            queue_result = f"skipped_state_{state.lower() or 'missing'}"
        return (
            f"episode_uuid: {normalized_uuid}\n"
            "lease_release: ok\n"
            f"queued: {queued}\n"
            f"queue_result: {queue_result}\n"
            f"state: {row.get('processing_state') if row else '(missing)'}\n"
            f"attempts: {int(row.get('processing_attempts') or 0) if row else 0}\n"
            f"error: {row.get('processing_error') if row else '(missing)'}"
        )
