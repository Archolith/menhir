"""MCP tool: run_llm_conflict_review."""

from __future__ import annotations

from menhir.mcp.tools.base import BaseJsonTool
from menhir.mcp.contracts import ToolScope


async def run_llm_conflict_review(limit: int = 20, namespace: str = "") -> str:
    """Run LLM contradiction confirmation on pending_llm_review conflicts immediately.

    Processes up to `limit` groups without waiting for the scheduler cycle.
    Each pair is sent to the LLM — confirmed contradictions become 'unresolved'
    (visible in list_conflicts), false positives are silently cleared.

    Args:
        limit: Max groups to process in this run (default 20).
        namespace: Optional silo to scope this operation to. Empty = every silo
            (existing behavior). A pinned client has this forced to its own silo.

    Returns:
        Counts of confirmed, cleared, and errors.
    """

    return await RunLLMReviewTool().execute(limit=limit, namespace=namespace)


class RunLLMReviewTool(BaseJsonTool):
    name = "run_llm_conflict_review"
    # NAMESPACED: this MUTATES conflict state on tenant nodes (promote to unresolved, or
    # clear as false_positive), and selected its work set with no tenancy predicate.
    scope = ToolScope.NAMESPACED
    required_tier = "operator"
    title = "Run LLM Conflict Review"
    oauth_scopes = ("menhir:admin",)
    # Mutates conflict state (promote to unresolved, or clear as false_positive).
    read_only_hint = False
    destructive_hint = False
    # Sends conflict pairs to the configured external LLM for confirmation.
    open_world_hint = True
    description = "Run LLM contradiction confirmation on pending conflicts."

    async def endpoint(self, limit: int = 20, namespace: str = "") -> str:
        backend = self.get_backend()
        # `namespace` is forwarded only when set. An unpinned caller therefore produces a
        # byte-identical backend call to the one made before this parameter existed --
        # not merely an equivalent query. That is the stronger property, and it is what
        # keeps every pre-existing backend stub and protocol implementation valid.
        scope = {"namespace": namespace} if namespace else {}
        counts = await backend.confirm_pending_conflicts(
            limit=limit, verbose=True, **scope
        )
        return self.render_json(counts)
