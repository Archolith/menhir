"""MCP tool: recall_history."""

from __future__ import annotations

from typing import Any

from menhir.mcp.tools.base import BaseJsonTool
from menhir.mcp.contracts import ToolScope


async def recall_history(
    query: str,
    namespace: str = "",
    limit: int = 30,
) -> str:
    """List saved memories matching a subject in time order (oldest first).

    Args:
        query: Subject to match. Natural language works best.
        namespace: Optional silo to scope this operation to. Empty = default/global behavior.
        limit: Max memories to return (default: 30, max: 50).
    """
    return await RecallHistoryTool().execute(
        query=query, namespace=namespace, limit=limit
    )


class RecallHistoryTool(BaseJsonTool):
    name = "recall_history"
    scope = ToolScope.NAMESPACED
    required_tier = "readonly"
    title = "Recall History"
    oauth_scopes = ("menhir:read",)
    read_only_hint = True
    destructive_hint = False
    open_world_hint = False
    description = (
        "List saved memories matching a subject in time order (oldest first). "
        "Use after recall_memories when a question needs how something "
        "changed, the sequence of states, or what it was before."
    )

    async def endpoint(
        self,
        query: str,
        namespace: str = "",
        limit: int = 30,
    ) -> str:
        """Drill-down over raw saved memories, oldest first. Use after recall_memories
        for how-something-changed, sequence-of-states, or what-it-was-before questions.

        namespace scopes the operation, subject to the client's configured pin.
        limit defaults to 30 (max 50).

        Read-only: no access updates. An empty result with a `note` means no embedded
        memory matched, not that no relevant memory exists.
        """
        backend = self.get_backend()
        try:
            result = await backend.recall_history(
                query,
                namespace=namespace or None,
                limit=limit,
            )
        except ValueError:
            return self.render_json(
                {
                    "ok": False,
                    "tool": self.operation,
                    "error": {"message": "A non-empty query is required."},
                }
            )
        payload: dict[str, Any] = {
            "query": result.get("query") or query,
            "count": len(result.get("memories", []) or []),
            "memories": [
                {
                    "time": m.get("reference_time"),
                    "content": m.get("content"),
                    "uuid": m.get("uuid"),
                    "source": m.get("source"),
                }
                for m in result.get("memories", []) or []
            ],
        }
        if result.get("note"):
            payload["note"] = result.get("note")
        return self.render_recall_json(payload)
