"""MCP tool: view_entropy — the D0 view-reachability probe over maintained Views."""

from __future__ import annotations

from menhir.mcp.tools.base import BaseJsonTool
from menhir.mcp.contracts import ToolScope


async def view_entropy(
    namespace: str = "",
    kind: str = "",
    top_k: int = 20,
    max_views: int = 50,
) -> str:
    """Deterministic retrieval-entropy probe: for each current View, the rank + token
    footprint at which recall returns the View's own canonical surface.

    A View is materialized query-sufficient state, so "retrieval reaches the View" is
    the label-free, LLM-free sufficiency check. Healthy = rank 1 / ~one surface of
    tokens; a rising rank or reached=False means the recall path is burying current
    state (ranking/supersession/stamping regression). Reachability only — it does not
    validate the View's value. The probe reads without reinforcing (no access-stat bumps).

    Args:
        namespace: Optional silo to probe. Empty = the default/shared namespace set.
        kind: Optional View kind filter (e.g. "counter", "timeline"). Empty = all kinds.
        top_k: How deep to search for each View before declaring it unreached (default: 20).
        max_views: Max Views to probe in one call (default: 50).
    """

    return await ViewEntropyTool().execute(
        namespace=namespace, kind=kind, top_k=top_k, max_views=max_views,
    )


class ViewEntropyTool(BaseJsonTool):
    name = "view_entropy"
    scope = ToolScope.NAMESPACED
    required_tier = "readonly"
    description = "D0 view-reachability probe: rank + footprint of each current View's self-recall."

    async def endpoint(
        self,
        namespace: str = "",
        kind: str = "",
        top_k: int = 20,
        max_views: int = 50,
    ) -> str:
        """Deterministic retrieval-entropy probe over the maintained Views.

        Args:
            namespace: Optional silo to probe. Empty = the default/shared namespace set.
            kind: Optional View kind filter (e.g. "counter", "timeline"). Empty = all kinds.
            top_k: How deep to search for each View before declaring it unreached (default: 20).
            max_views: Max Views to probe in one call (default: 50).

        Returns:
            JSON with a summary (views_probed, reached, median rank/tokens) and per-View rows.
        """
        backend = self.get_backend()
        result = await backend.view_entropy(
            namespace=namespace or None,
            kind=kind or None,
            top_k=top_k,
            max_views=max_views,
        )
        return self.render_json({"tool": self.operation, **(result or {})})
