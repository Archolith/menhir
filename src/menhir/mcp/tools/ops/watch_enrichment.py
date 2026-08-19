"""MCP tool: watch_enrichment."""

from __future__ import annotations

from menhir.mcp.formatters import _collect_episode_status, _format_episode_watch, _require_episode_uuid
from menhir.mcp.tools.base import BaseTextTool
from menhir.mcp.contracts import ToolScope


async def watch_enrichment(
    episode_uuid: str,
    timeout_s: float = 120.0,
    poll_interval_s: float = 1.0,
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
    )


class WatchEnrichmentTool(BaseTextTool):
    name = "watch_enrichment"
    scope = ToolScope.OBJECT
    required_tier = "readonly"
    description = "Follow one enrichment live and return observed deltas."

    def timeout_for(
        self,
        episode_uuid: str,
        timeout_s: float = 120.0,
        poll_interval_s: float = 1.0,
    ) -> int:
        return max(30, int(timeout_s) + 5)

    async def endpoint(
        self,
        episode_uuid: str,
        timeout_s: float = 120.0,
        poll_interval_s: float = 1.0,
    ) -> str:
        backend = self.get_backend()
        normalized_uuid = _require_episode_uuid(episode_uuid)
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
