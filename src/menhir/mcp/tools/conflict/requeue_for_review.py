"""MCP tool: requeue_conflicts_for_llm_review."""

from __future__ import annotations

from menhir.mcp.tools.base import BaseJsonTool
from menhir.mcp.contracts import ToolScope


async def requeue_conflicts_for_llm_review(
    from_status: str = "unresolved",
    limit: int = 200,
    namespace: str = "",
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
        namespace: Optional silo to scope this operation to. Empty = every silo
            (existing behavior). A pinned client has this forced to its own silo.

    Returns:
        Count of groups re-queued.
    """

    return await RequeueForReviewTool().execute(
        from_status=from_status, limit=limit, namespace=namespace
    )


class RequeueForReviewTool(BaseJsonTool):
    name = "requeue_conflicts_for_llm_review"
    # NAMESPACED: this MUTATES conflict_status on tenant nodes and selected them with no
    # tenancy predicate -- the same shape as CF-217.
    scope = ToolScope.NAMESPACED
    required_tier = "operator"
    description = "Re-queue conflict groups for LLM contradiction confirmation."

    async def endpoint(
        self,
        from_status: str = "unresolved",
        limit: int = 200,
        namespace: str = "",
    ) -> str:
        backend = self.get_backend()
        # `namespace` is forwarded only when set. An unpinned caller therefore produces a
        # byte-identical backend call to the one made before this parameter existed --
        # not merely an equivalent query. That is the stronger property, and it is what
        # keeps every pre-existing backend stub and protocol implementation valid.
        scope = {"namespace": namespace} if namespace else {}
        requeued = await backend.requeue_conflicts_for_llm_review(
            from_status=from_status,
            limit=limit,
            **scope,
        )
        return self.render_json({"requeued": requeued, "from_status": from_status})
