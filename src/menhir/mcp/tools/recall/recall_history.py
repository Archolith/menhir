"""MCP tool: recall_history."""

from __future__ import annotations

from typing import Any

from menhir.mcp.tools.base import BaseJsonTool
from menhir.mcp.contracts import ToolScope
from menhir.mcp.namespace_note import namespace_note
from menhir.mcp.telemetry.tracker import McpToolRefusal

RECALL_HISTORY_MAX = 50


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
        # #670: the server clamps to 1..50; say so instead of returning fewer than asked.
        applied = max(1, min(int(limit), RECALL_HISTORY_MAX))
        backend = self.get_backend()
        try:
            result = await backend.recall_history(
                query,
                namespace=namespace or None,
                limit=applied,
            )
        except McpToolRefusal:
            raise
        except ValueError as exc:
            raise McpToolRefusal(str(exc), code="invalid_argument") from exc
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
        if applied != limit:
            payload["limit_applied"] = applied
        if result.get("note"):
            payload["note"] = result.get("note")
        if not payload["memories"]:
            # #671: an empty result under a namespace that holds nothing is not "no history".
            ns_note = await namespace_note(backend, namespace)
            if ns_note:
                payload["namespace_note"] = ns_note
        return self.render_recall_json(payload)
