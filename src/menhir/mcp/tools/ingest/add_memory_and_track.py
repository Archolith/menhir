"""MCP tool: add_memory_and_track."""

from __future__ import annotations

from menhir.mcp.formatters import _collect_episode_status, _format_episode_status, _queue_summary
from menhir.mcp.service_access import get_mcp_session
from menhir.mcp.tools.base import BaseTextTool
from menhir.mcp.contracts import ToolScope


async def add_memory_and_track(
    text: str,
    source: str = "claude-code",
    timeout_s: float = 60.0,
    poll_interval_s: float = 1.0,
    diff: str | None = None,
    turn_evidence_uuid: str | None = None,
    namespace: str = "",
) -> str:
    """Queue one memory and return enrichment status updates until READY/FAILED/timeout.

    Args:
        text: Memory content to ingest.
        source: Source provenance label.
        timeout_s: Max seconds to wait for enrichment completion.
        poll_interval_s: Status polling interval in seconds.
        diff: Optional git diff to attach as context for what changed alongside this memory.
        turn_evidence_uuid: Optional UUID of the :TurnEvidence node for the turn this memory was written in the context of. For source='user'/'manual' it also grounds the user-tier claim (an ungrounded claim is downgraded). For every other source it draws the provenance edge only, which is what lets a typed-scalar assertion reach the entities extracted from this memory -- pass it whenever you know it.

    Returns:
        Episode status summary plus observed state transitions.
    """

    return await AddMemoryAndTrackTool().execute(
        text=text,
        source=source,
        timeout_s=timeout_s,
        poll_interval_s=poll_interval_s,
        diff=diff,
        turn_evidence_uuid=turn_evidence_uuid,
        namespace=namespace,
    )


class AddMemoryAndTrackTool(BaseTextTool):
    name = "add_memory_and_track"
    scope = ToolScope.NAMESPACED
    title = "Add Memory And Track"
    oauth_scopes = ("menhir:write",)
    read_only_hint = False
    destructive_hint = False
    open_world_hint = True
    description = "Queue one memory and track enrichment until completion."

    def timeout_for(
        self,
        text: str,
        source: str = "claude-code",
        timeout_s: float = 60.0,
        poll_interval_s: float = 1.0,
        diff: str | None = None,
        turn_evidence_uuid: str | None = None,
        namespace: str = "",
    ) -> int:
        return max(30, int(timeout_s) + 5)

    async def endpoint(
        self,
        text: str,
        source: str = "claude-code",
        timeout_s: float = 60.0,
        poll_interval_s: float = 1.0,
        diff: str | None = None,
        turn_evidence_uuid: str | None = None,
        namespace: str = "",
    ) -> str:
        """Queue one memory and return enrichment status updates until READY/FAILED/timeout.

        Args:
            text: Memory content to ingest.
            source: Source provenance label.
            timeout_s: Max seconds to wait for enrichment completion.
            poll_interval_s: Status polling interval in seconds.
            diff: Optional git diff to attach as context for what changed alongside this memory.
            turn_evidence_uuid: Optional UUID of the :TurnEvidence node for the turn this memory was written in the context of. For source='user'/'manual' it also grounds the user-tier claim (an ungrounded claim is downgraded). For every other source it draws the provenance edge only, which is what lets a typed-scalar assertion reach the entities extracted from this memory -- pass it whenever you know it.
            namespace: Silo to write into. A pinned client has this forced, and the parameter
                existing is what makes that possible -- the pin is injected only into endpoints
                whose signature declares it.

        Returns:
            Episode status summary plus observed state transitions.
        """
        backend = self.get_backend()
        session = get_mcp_session()
        queued = await backend.queue_episode(
            text,
            user_id=session.user_id,
            session_id=session.session_id,
            source=source,
            diff=diff,
            turn_evidence_uuid=turn_evidence_uuid,
            # CF-220: `add_memory` declares `namespace` and is therefore pinnable; this tool
            # performs the SAME write and did not, so a pinned client escaped its pin simply by
            # calling the sibling. `queue_episode` accepted the argument all along.
            namespace=namespace or None,
        )
        if str(queued.get("status") or "") == "failed":
            return "Failed to queue memory."
        episode_uuid = str(queued.get("episode_id") or "")
        row, history, timed_out = await _collect_episode_status(
            backend,
            episode_uuid,
            timeout_s=timeout_s,
            poll_interval_s=poll_interval_s,
        )
        episode_status = _format_episode_status(
            episode_uuid=episode_uuid,
            row=row,
            history=history,
            timed_out=timed_out,
        )
        return f"queued_summary: {await _queue_summary(backend)}\n{episode_status}"
