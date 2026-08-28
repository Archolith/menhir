"""MCP tool: force_reenrich."""

from __future__ import annotations

from typing import Any

from menhir.mcp.formatters import _collect_episode_status, _format_episode_status, _require_episode_uuid
from menhir.mcp.ownership import foreign_object_refusal
from menhir.mcp.tools.base import BaseTextTool
from menhir.mcp.contracts import ToolScope


async def force_reenrich(
    episode_uuid: str,
    wait: bool = True,
    timeout_s: float = 300.0,
    poll_interval_s: float = 2.0,
    namespace: str = "",
) -> str:
    """Force a failed episode back into enrichment and track it live.

    Resets processing_attempts and processing_error, pushes the episode
    to the front of the enrichment queue, then polls until READY/FAILED
    or timeout.

    Args:
        episode_uuid: UUID of the failed episode to re-enrich.
        wait: If true (default), poll until enrichment completes or times out.
        timeout_s: Max seconds to wait when wait=true (default: 300).
        poll_interval_s: Poll interval in seconds (default: 2).

    Returns:
        Live enrichment status including state transitions observed.
    """

    return await ForceReenrichTool().execute(
        episode_uuid=episode_uuid,
        wait=wait,
        timeout_s=timeout_s,
        poll_interval_s=poll_interval_s,
        namespace=namespace,
    )


class ForceReenrichTool(BaseTextTool):
    name = "force_reenrich"
    # NAMESPACED once the ownership guard exists: the pin can now reach this tool,
    # and the uuid it addresses is checked against that pin at load (CF-33 step 4).
    scope = ToolScope.NAMESPACED
    required_tier = "operator"
    title = "Force Re-enrich Episode"
    oauth_scopes = ("menhir:admin",)
    read_only_hint = False
    destructive_hint = False
    open_world_hint = True
    description = "Force a failed episode back into enrichment."

    def timeout_for(
        self,
        episode_uuid: str,
        wait: bool = True,
        timeout_s: float = 300.0,
        poll_interval_s: float = 2.0,
        # Absorbs endpoint parameters that do not affect the timeout. `timeout_for` is called
        # with the endpoint's own kwargs, so a signature that enumerates them breaks the tool
        # the moment the endpoint gains an argument -- as adding `namespace` did.
        **_unused: Any,
    ) -> int:
        return max(60, int(timeout_s) + 10) if wait else 30

    async def endpoint(
        self,
        episode_uuid: str,
        wait: bool = True,
        timeout_s: float = 300.0,
        poll_interval_s: float = 2.0,
        namespace: str = "",
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

        reset_ok = await backend.force_reset_failed_episode(normalized_uuid)
        if not reset_ok:
            return f"episode_uuid: {normalized_uuid}\nresult: NOT_FOUND — no FAILED/PENDING episode with that UUID"

        queued = await backend.enqueue_pending_episode(normalized_uuid)

        if not wait:
            return (
                f"episode_uuid: {normalized_uuid}\n"
                f"reset: ok\n"
                f"queued: {queued}\n"
                f"result: submitted — use get_enrichment_status to track"
            )

        row, history, timed_out = await _collect_episode_status(
            backend,
            normalized_uuid,
            timeout_s=timeout_s,
            poll_interval_s=poll_interval_s,
        )
        return _format_episode_status(
            episode_uuid=normalized_uuid,
            row=row,
            history=history,
            timed_out=timed_out,
        )
