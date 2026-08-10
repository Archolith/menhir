"""MCP tool: run_llm_conflict_review."""

from __future__ import annotations

from menhir.mcp.tools.base import BaseJsonTool


async def run_llm_conflict_review(limit: int = 20) -> str:
    """Run LLM contradiction confirmation on pending_llm_review conflicts immediately.

    Processes up to `limit` groups without waiting for the scheduler cycle.
    Each pair is sent to the LLM — confirmed contradictions become 'unresolved'
    (visible in list_conflicts), false positives are silently cleared.

    Args:
        limit: Max groups to process in this run (default 20).

    Returns:
        Counts of confirmed, cleared, and errors.
    """

    return await RunLLMReviewTool().execute(limit=limit)


class RunLLMReviewTool(BaseJsonTool):
    name = "run_llm_conflict_review"
    required_tier = "operator"
    description = "Run LLM contradiction confirmation on pending conflicts."

    async def endpoint(self, limit: int = 20) -> str:
        backend = self.get_backend()
        counts = await backend.confirm_pending_conflicts(limit=limit, verbose=True)
        return self.render_json(counts)
