"""MCP tool: recall_timeline."""

from __future__ import annotations

from typing import Any

from menhir.mcp.tools.base import BaseJsonTool
from menhir.mcp.contracts import ToolScope


async def recall_timeline(
    namespace: str = "",
    query: str | None = None,
    subject: str | None = None,
    at: str | None = None,
    window_from: str | None = None,
    window_to: str | None = None,
    around: str | None = None,
    cursor: str | None = None,
    direction: str = "both",
    limit: int = 10,
    detail: str = "headline",
    facts: bool = False,
    history_view: str | None = None,
    history_offset: int | None = None,
    history_limit: int = 10,
) -> str:
    """Navigate a recorded-time thread of saved memories on demand.

    Args:
        namespace: Optional silo to scope this operation to. Empty = default/global behavior.
        query: Semantic seed: the timeline anchors on the best-matching memory.
        subject: Entity name or uuid; restricts the timeline to that entity's thread.
        at: ISO instant to split the timeline around.
        window_from: ISO instant starting an ascending time window.
        window_to: ISO instant ending an ascending time window.
        around: Memory uuid to anchor the page on.
        cursor: Opaque cursor from a previous page (prev_cursor / next_cursor).
        direction: "before", "after", or "both" (default).
        limit: Max entries per page (default: 10, max: 50).
        detail: "headline" (default) or "full" content.
        facts: Attach RELATES_TO facts to the page's entries (default: false).
        history_view: Page only this typed-history view uuid.
        history_offset: Offset into the typed-history entries instead of the latest page.
        history_limit: Max typed-history entries per view (default: 10, max: 50).
    """
    return await RecallTimelineTool().execute(
        namespace=namespace,
        query=query,
        subject=subject,
        at=at,
        window_from=window_from,
        window_to=window_to,
        around=around,
        cursor=cursor,
        direction=direction,
        limit=limit,
        detail=detail,
        facts=facts,
        history_view=history_view,
        history_offset=history_offset,
        history_limit=history_limit,
    )


class RecallTimelineTool(BaseJsonTool):
    name = "recall_timeline"
    scope = ToolScope.NAMESPACED
    required_tier = "readonly"
    title = "Recall Timeline"
    oauth_scopes = ("menhir:read",)
    read_only_hint = True
    destructive_hint = False
    open_world_hint = False
    description = (
        "Navigate a recorded-time thread of saved memories on demand: page around an "
        "instant, a window, a memory, or a semantic seed, optionally restricted to one "
        "subject's thread, with typed scalar/event histories. Use when a question needs "
        "what was known at a time or how a thread unfolded."
    )

    async def endpoint(
        self,
        namespace: str = "",
        query: str | None = None,
        subject: str | None = None,
        at: str | None = None,
        window_from: str | None = None,
        window_to: str | None = None,
        around: str | None = None,
        cursor: str | None = None,
        direction: str = "both",
        limit: int = 10,
        detail: str = "headline",
        facts: bool = False,
        history_view: str | None = None,
        history_offset: int | None = None,
        history_limit: int = 10,
    ) -> str:
        """On-demand time navigation over recorded memory time (RECORDED time, not
        world time). Exactly one of at / window_from+window_to / around / cursor /
        query is required when any is given; subject is a filter that combines with
        every mode and alone means the latest page of the subject thread.

        namespace scopes the operation, subject to the client's configured pin.
        Read-only: no access updates.
        """
        backend = self.get_backend()
        try:
            result = await backend.recall_timeline(
                namespace=namespace or None,
                query=query,
                subject=subject,
                at=at,
                window_from=window_from,
                window_to=window_to,
                around=around,
                cursor=cursor,
                direction=direction,
                limit=limit,
                detail=detail,
                facts=facts,
                history_view=history_view,
                history_offset=history_offset,
                history_limit=history_limit,
            )
        except ValueError as exc:
            return self.render_json(
                {
                    "ok": False,
                    "tool": self.operation,
                    "error": {"message": str(exc)},
                }
            )
        payload: dict[str, Any] = {
            "thread": result.get("thread"),
            "count": len(result.get("entries", []) or []),
            "entries": [
                {
                    "recorded_at": e.get("recorded_at"),
                    "headline": e.get("headline"),
                    **(
                        {"content": e.get("content")}
                        if e.get("content") is not None
                        else {}
                    ),
                    "uuid": e.get("uuid"),
                    "source": e.get("source"),
                    **({"is_anchor": True} if e.get("is_anchor") else {}),
                    **(
                        {
                            "facts": [
                                {
                                    "fact": f.get("fact"),
                                    "valid_at": f.get("valid_at"),
                                }
                                for f in (e.get("facts") or [])
                            ]
                        }
                        if e.get("facts")
                        else {}
                    ),
                }
                for e in result.get("entries", []) or []
            ],
        }
        if result.get("subject_uuid"):
            payload["subject"] = {
                "uuid": result.get("subject_uuid"),
                "name": result.get("subject_name"),
            }
        if result.get("prev_cursor"):
            payload["prev_cursor"] = result.get("prev_cursor")
        if result.get("next_cursor"):
            payload["next_cursor"] = result.get("next_cursor")
        if result.get("histories"):
            payload["histories"] = [
                {
                    "kind": h.get("kind"),
                    "label": h.get("label"),
                    "total": h.get("total"),
                    "offset": h.get("offset"),
                    "view_uuid": h.get("view_uuid"),
                    **({"prev_offset": h.get("prev_offset")} if h.get("prev_offset") is not None else {}),
                    **({"next_offset": h.get("next_offset")} if h.get("next_offset") is not None else {}),
                    "entries": [
                        {
                            "valid_at": he.get("valid_at"),
                            "value": he.get("value"),
                            **({"operation": he.get("operation")} if he.get("operation") else {}),
                            **({"time_basis": he.get("time_basis")} if he.get("time_basis") else {}),
                            **({"quote": he.get("quote")} if he.get("quote") else {}),
                        }
                        for he in (h.get("entries") or [])
                    ],
                }
                for h in result.get("histories", []) or []
            ]
        if result.get("note"):
            payload["note"] = result.get("note")
        return self.render_recall_json(payload)
