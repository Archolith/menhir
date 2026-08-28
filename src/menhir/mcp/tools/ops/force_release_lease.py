"""MCP tool: force_release_enrichment_lease."""

from __future__ import annotations

from menhir.mcp.formatters import _require_episode_uuid
from menhir.mcp.telemetry import record_lifecycle_event
from menhir.mcp.ownership import foreign_object_refusal
from menhir.mcp.tools.base import BaseTextTool
from menhir.mcp.contracts import ToolScope


async def force_release_enrichment_lease(
    episode_uuid: str, requeue: bool = True, namespace: str = ""
) -> str:
    """Force-release one ENRICHING episode lease, failing exhausted rows instead of requeueing them."""

    return await ForceReleaseEnrichmentLeaseTool().execute(
        episode_uuid=episode_uuid, requeue=requeue, namespace=namespace
    )


class ForceReleaseEnrichmentLeaseTool(BaseTextTool):
    name = "force_release_enrichment_lease"
    # NAMESPACED once the ownership guard exists: the pin can now reach this tool,
    # and the uuid it addresses is checked against that pin at load (CF-33 step 4).
    scope = ToolScope.NAMESPACED
    required_tier = "operator"
    title = "Force Release Enrichment Lease"
    oauth_scopes = ("menhir:admin",)
    read_only_hint = False
    destructive_hint = False
    open_world_hint = False
    description = "Force-release one ENRICHING episode lease."

    async def endpoint(
        self, episode_uuid: str, requeue: bool = True, namespace: str = ""
    ) -> str:
        backend = self.get_backend()
        normalized_uuid = _require_episode_uuid(episode_uuid)
        # CF-33 step 4: ownership-at-load. An episode uuid is not proof of ownership -- a
        # pinned client that learned one through any global read could previously inspect,
        # re-enrich or release the lease on another silo's episode. Two lookups, per CF-64:
        # only an episode that demonstrably belongs elsewhere is refused, so absent-episode
        # paths keep reporting "not found" rather than "refused".
        refusal = await foreign_object_refusal(
            uuid=normalized_uuid,
            namespace=namespace,
            # Resolved lazily so an UNPINNED call touches nothing it did not touch before:
            # `backend.fetch_memory_by_uuid` is not even looked up unless a namespace is set.
            lookup=lambda uuid, **kw: backend.fetch_memory_by_uuid(uuid, **kw),
            label="episode",
        )
        if refusal:
            return refusal

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
