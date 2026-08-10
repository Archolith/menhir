"""MCP tool: force_reenrich."""

from __future__ import annotations

from menhir.mcp.formatters import _collect_episode_status, _format_episode_status, _require_episode_uuid
from menhir.mcp.tools.base import BaseTextTool


async def force_reenrich(
    episode_uuid: str,
    wait: bool = True,
    timeout_s: float = 300.0,
    poll_interval_s: float = 2.0,
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
    )


class ForceReenrichTool(BaseTextTool):
    name = "force_reenrich"
    required_tier = "operator"
    description = "Force a failed episode back into enrichment."

    def timeout_for(
        self,
        episode_uuid: str,
        wait: bool = True,
        timeout_s: float = 300.0,
        poll_interval_s: float = 2.0,
    ) -> int:
        return max(60, int(timeout_s) + 10) if wait else 30

    async def endpoint(
        self,
        episode_uuid: str,
        wait: bool = True,
        timeout_s: float = 300.0,
        poll_interval_s: float = 2.0,
    ) -> str:
        backend = self.get_backend()
        normalized_uuid = _require_episode_uuid(episode_uuid)

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
