"""MCP tool: build_context."""

from __future__ import annotations

from datetime import datetime, timezone

from menhir.domain.recall import InvalidQueryPresetError, format_query_preset_values
from menhir.mcp.service_access import get_request_session
from menhir.mcp.tools.base import BaseTextTool


def _build_temporal_header() -> str | None:
    """Return a temporal context line for the current caller, or None.

    Keyed by session_id so each conversation/window has independent tracking.
    """
    session = get_request_session()
    if session is None or not session.session_id:
        return None
    try:
        from menhir.mcp.telemetry import telemetry_store
        last_accessed = telemetry_store.get_session_last_accessed(session.session_id)
        now = datetime.now(timezone.utc)
        now_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        if not last_accessed:
            return f"_{now_str} — first session_"
        last_dt = datetime.fromisoformat(last_accessed)
        delta = now - last_dt
        hours = delta.total_seconds() / 3600
        if hours < 1:
            elapsed = f"{int(delta.total_seconds() / 60)}m"
        elif hours < 48:
            elapsed = f"{hours:.1f}h"
        else:
            elapsed = f"{delta.days}d"
        return f"_{now_str} — {elapsed} since last session_"
    except Exception:
        return None


async def build_context(
    query: str,
    max_tokens: int = 2000,
    preset: str = "knowledge",
    session_id: str | None = None,
    include_scores: bool = False,
    namespace: str = "",
) -> str:
    """Build a token-budget-limited context string from recalled memories.

    Args:
        query: Natural language query to recall relevant memories.
        max_tokens: Token budget for the returned context (default: 2000).
        preset: Recall preset — one of 'knowledge', 'recent', 'emotional', 'connected', or 'conflict' (default: 'knowledge').
        session_id: Optional session ID for session-scoped recall.
        include_scores: Include relevance scores in output (default: False).
        namespace: Optional silo to scope this operation to. Empty = default/global behavior.
    """

    return await BuildContextTool().execute(
        query=query,
        max_tokens=max_tokens,
        preset=preset,
        session_id=session_id,
        include_scores=include_scores,
        namespace=namespace,
    )


class BuildContextTool(BaseTextTool):
    name = "build_context"
    required_tier = "readonly"
    description = "Build a token-budget-limited context string."

    async def endpoint(
        self,
        query: str,
        max_tokens: int = 2000,
        preset: str = "knowledge",
        session_id: str | None = None,
        include_scores: bool = False,
        namespace: str = "",
    ) -> str:
        """Build a token-budget-limited context string from recalled memories."""
        backend = self.get_backend()
        try:
            result = await backend.build_context(
                query,
                max_tokens=max_tokens,
                preset=preset,
                session_id=session_id,
                include_scores=include_scores,
                namespace=namespace or None,
            )
        except InvalidQueryPresetError:
            return (
                f"Invalid preset '{preset}'. Use: {format_query_preset_values()}."
            )

        # Temporal header — client-id-specific last_accessed lookup
        temporal_header = _build_temporal_header()

        lines: list[str] = []
        if temporal_header:
            lines.append(temporal_header)
            lines.append("")
        lines.append(str(result.get("context") or ""))
        lines.append("")
        lines.append("---")
        lines.append(
            f"Memories: {int(result.get('memory_count') or 0)} | "
            f"Tokens: ~{int(result.get('token_estimate') or 0)} ({result.get('estimation_mode') or 'unknown'})"
        )
        if bool(result.get("truncated")):
            lines.append("(truncated to fit budget)")
        from menhir.mcp.feedback import recall_receipt_footer

        footer = recall_receipt_footer(self.operation)
        if footer:
            lines.append(footer)
        return "\n".join(lines)
