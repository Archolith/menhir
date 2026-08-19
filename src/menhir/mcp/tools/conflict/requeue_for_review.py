"""MCP tool: requeue_conflicts_for_llm_review."""

from __future__ import annotations

from menhir.mcp.tools.base import BaseJsonTool
from menhir.mcp.contracts import ToolScope


async def requeue_conflicts_for_llm_review(
    from_status: str = "unresolved",
    limit: int = 200,
) -> str:
    """Re-queue conflict groups for LLM contradiction confirmation.

    Sets conflict_status back to 'pending_llm_review' so the scheduler's
    confirm_conflicts job will run LLM verification on each pair. Use this
    to backfill existing 'unresolved' conflicts that pre-date LLM confirmation,
    or to re-check previously cleared groups.

    Args:
        from_status: Source status to re-queue ("unresolved", "false_positive",
                     "auto-resolved"). Defaults to "unresolved".
        limit: Max groups to re-queue (default 200).

    Returns:
        Count of groups re-queued.
    """

    return await RequeueForReviewTool().execute(from_status=from_status, limit=limit)


class RequeueForReviewTool(BaseJsonTool):
    name = "requeue_conflicts_for_llm_review"
    scope = ToolScope.OBJECT
    required_tier = "operator"
    description = "Re-queue conflict groups for LLM contradiction confirmation."

    async def endpoint(
        self,
        from_status: str = "unresolved",
        limit: int = 200,
    ) -> str:
        backend = self.get_backend()
        requeued = await backend.requeue_conflicts_for_llm_review(
            from_status=from_status,
            limit=limit,
        )
        return self.render_json({"requeued": requeued, "from_status": from_status})
