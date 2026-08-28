"""MCP tool: recall_memories."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from types import SimpleNamespace

from menhir.mcp.formatters import _compact_scored_item
from menhir.mcp.service_access import get_request_session
from menhir.mcp.tools.base import BaseJsonTool
from menhir.mcp.contracts import ToolScope


def _resolve_compact(explicit: bool | None) -> bool:
    """Resolve compact mode: explicit arg wins, else env default, else False."""
    if explicit is not None:
        return explicit
    return os.environ.get("MENHIR_RECALL_COMPACT", "").lower() in ("1", "true", "yes")


async def recall_memories(
    query: str,
    preset: str = "knowledge",
    limit: int = 5,
    file_context: str = "",
    file_context_project: str = "",
    namespace: str = "",
    include_invalidated: bool = False,
    compact: bool | None = None,
    trace: bool = False,
) -> str:
    """Search memories by semantic similarity. Returns ranked results with relevance scores.

    Args:
        query: What to search for. Natural language works best.
        preset: Ranking strategy — "knowledge" (default), "recent", "connected". Also accepts "emotional" and "conflict" (partial — weight balance only, no domain signals yet).
        limit: Max results to return (default: 5).
        file_context: Optional file path — boosts memories linked to this file and its structural neighbors. Accepts absolute or relative paths.
        file_context_project: Optional project name for file_context disambiguation. Auto-detected if omitted.
        namespace: Optional silo to scope this operation to. Empty = default/global behavior.
        include_invalidated: When True, also return superseded/historical beliefs (expired facts). Default False = current beliefs only.
        compact: True to drop per-item explainability (breakdown sub-scores, type) and candidates_evaluated, keeping only the decision-relevant fields (name, scope, score, relevance, summary, uuid). None defers to the MENHIR_RECALL_COMPACT env default (off).

    Returns:
        Ranked memory results with scores and explainability breakdown.
    """

    return await RecallMemoriesTool().execute(
        query=query, preset=preset, limit=limit,
        file_context=file_context, file_context_project=file_context_project,
        namespace=namespace,
        include_invalidated=include_invalidated,
        _compact=compact,
        trace=trace,
    )


class RecallMemoriesTool(BaseJsonTool):
    name = "recall_memories"
    scope = ToolScope.NAMESPACED
    required_tier = "readonly"
    title = "Recall Memories"
    oauth_scopes = ("menhir:read",)
    read_only_hint = True
    destructive_hint = False
    open_world_hint = False
    description = "Search memories by semantic similarity."

    async def endpoint(
        self,
        query: str,
        preset: str = "knowledge",
        limit: int = 5,
        file_context: str = "",
        file_context_project: str = "",
        namespace: str = "",
        include_invalidated: bool = False,
        compact: bool | None = None,
        trace: bool = False,
    ) -> str:
        """Search memories by semantic similarity. Returns ranked results with relevance scores.

        Args:
            query: What to search for. Natural language works best.
            preset: Ranking strategy — "knowledge" (default), "recent", "connected". Also accepts "emotional" and "conflict" (partial — weight balance only, no domain signals yet).
            limit: Max results to return (default: 5).
            file_context: Optional file path — boosts memories linked to this file and its structural neighbors.
            file_context_project: Optional project name for file_context disambiguation.
            namespace: Optional silo to scope this operation to. Empty = default/global behavior.
            include_invalidated: When True, also return superseded/historical beliefs (expired facts). Default False = current beliefs only.

        Returns:
            Ranked memory results with scores and explainability breakdown.
        """
        backend = self.get_backend()
        try:
            result = await backend.recall(
                query,
                preset=preset,
                limit=limit,
                include_session=True,
                wait_for_pending=True,
                file_context=file_context or None,
                file_context_project=file_context_project or None,
                namespace=namespace or None,
                include_invalidated=include_invalidated,
                trace=trace,
            )
        except ValueError:
            return self.render_json(
                {
                    "ok": False,
                    "tool": self.operation,
                    "error": {
                        "message": (
                            f"Invalid preset '{preset}'. Use: knowledge, recent, emotional, connected, conflict."
                        )
                    },
                }
            )
        now = datetime.now(timezone.utc)
        retrieved_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        elapsed_since_last_access: str | None = None

        session = get_request_session()
        if session is not None and session.session_id:
            try:
                from menhir.mcp.telemetry import telemetry_store
                last_accessed = telemetry_store.get_session_last_accessed(session.session_id)
                if last_accessed:
                    last_dt = datetime.fromisoformat(last_accessed)
                    delta = now - last_dt
                    hours = delta.total_seconds() / 3600
                    if hours < 1:
                        elapsed_since_last_access = f"{int(delta.total_seconds() / 60)}m"
                    elif hours < 48:
                        elapsed_since_last_access = f"{hours:.1f}h"
                    else:
                        elapsed_since_last_access = f"{delta.days}d"
            except Exception:
                pass

        compact = _resolve_compact(compact)
        payload: dict = {
            "retrieved_at": retrieved_at,
            "query": query,
            "preset": result.get("preset"),
            "count": len(result.get("results", [])),
            "items": [
                _compact_scored_item(SimpleNamespace(**scored), compact=compact)
                for scored in result.get("results", [])
            ],
        }
        if not compact:
            payload["candidates_evaluated"] = result.get("candidates_evaluated", 0)
        if result.get("note"):
            payload["note"] = result.get("note")
        if result.get("authority_layer"):
            # Decision-relevant and already bounded by recall (7.J), so compact mode retains it.
            payload["authority_layer"] = result.get("authority_layer")
        if result.get("event_authority_layer"):
            # Decision-relevant and already bounded by recall (7.J), so compact mode retains it.
            payload["event_authority_layer"] = result.get("event_authority_layer")
        if trace and result.get("trace") is not None:
            payload["trace"] = result.get("trace")
        if elapsed_since_last_access is not None:
            payload["elapsed_since_last_access"] = elapsed_since_last_access
        return self.render_recall_json(payload)
