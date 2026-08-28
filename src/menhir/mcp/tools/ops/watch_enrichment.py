"""MCP tool: watch_enrichment."""

from __future__ import annotations

from typing import Any

from menhir.mcp.formatters import _collect_episode_status, _format_episode_watch, _require_episode_uuid
from menhir.mcp.ownership import foreign_object_refusal
from menhir.mcp.tools.base import BaseTextTool
from menhir.mcp.contracts import ToolScope


async def watch_enrichment(
    episode_uuid: str,
    timeout_s: float = 120.0,
    poll_interval_s: float = 1.0,
    namespace: str = "",
) -> str:
    """Follow one enrichment live and return only observed deltas until terminal state or timeout.

    Args:
        episode_uuid: Episode UUID to watch.
        timeout_s: Max wait time before returning the latest observed state.
        poll_interval_s: Poll interval in seconds.

    Returns:
        Delta-oriented enrichment watch output.
    """

    return await WatchEnrichmentTool().execute(
        episode_uuid=episode_uuid,
        timeout_s=timeout_s,
        poll_interval_s=poll_interval_s,
        namespace=namespace,
    )


class WatchEnrichmentTool(BaseTextTool):
    name = "watch_enrichment"
    # NAMESPACED once the ownership guard exists: the pin can now reach this tool,
    # and the uuid it addresses is checked against that pin at load (CF-33 step 4).
    scope = ToolScope.NAMESPACED
    required_tier = "readonly"
    description = "Follow one enrichment live and return observed deltas."
    title = "Watch Enrichment"
    oauth_scopes = ("menhir:read",)
    read_only_hint = True
    destructive_hint = False
    open_world_hint = False

    def timeout_for(
        self,
        episode_uuid: str,
        timeout_s: float = 120.0,
        poll_interval_s: float = 1.0,
        # Absorbs endpoint parameters that do not affect the timeout. `timeout_for` is called
        # with the endpoint's own kwargs, so a signature that enumerates them breaks the tool
        # the moment the endpoint gains an argument -- as adding `namespace` did.
        **_unused: Any,
    ) -> int:
        return max(30, int(timeout_s) + 5)

    async def endpoint(
        self,
        episode_uuid: str,
        timeout_s: float = 120.0,
        poll_interval_s: float = 1.0,
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
        row, history, timed_out = await _collect_episode_status(
            backend,
            normalized_uuid,
            timeout_s=timeout_s,
            poll_interval_s=poll_interval_s,
        )
        return _format_episode_watch(
            episode_uuid=normalized_uuid,
            row=row,
            history=history,
            timed_out=timed_out,
        )
