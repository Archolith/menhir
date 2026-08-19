"""MCP tool: recall_context_memories."""

from __future__ import annotations

from menhir.domain.bootstrap_scope import bootstrap_selection
from menhir.domain.structural_memory import is_structural_memory_row
from menhir.mcp.formatters import _compact_memory_item, _normalize_reader_id
from menhir.mcp.lifecycle import _has_recent_flagged_bootstrap_read
from menhir.mcp.tools.base import BaseJsonTool
from menhir.mcp.contracts import ToolScope


async def recall_context_memories(
    reader_id: str = "default",
    query: str = "",
    preset: str = "knowledge",
    limit: int = 5,
    recent_limit: int = 5,
    namespace: str = "",
    workspace: str = "",
) -> str:
    """Read non-flagged startup context memories (recent + relevant).

    This call requires a prior `read_flagged_memories(reader_id=...)` for the
    current flagged-memory version.

    Args:
        reader_id: Stable bot/client identifier used for bootstrap gating.
        query: Optional natural-language query for relevant context retrieval.
        preset: Ranking preset for relevant retrieval when query is provided.
        limit: Max relevant results to return (default: 5).
        recent_limit: Max recent context rows to include (default: 5).
        namespace: Optional silo to scope this operation to. Empty = default/global behavior.
        workspace: Registered key selecting general + workspace bootstrap pins.

    Returns:
        Context bundle with relevant and recent non-flagged memories.
    """

    return await RecallContextMemoriesTool().execute(
        reader_id=reader_id,
        query=query,
        preset=preset,
        limit=limit,
        recent_limit=recent_limit,
        namespace=namespace,
        workspace=workspace,
    )


class RecallContextMemoriesTool(BaseJsonTool):
    name = "recall_context_memories"
    scope = ToolScope.NAMESPACED
    required_tier = "readonly"
    description = "Read non-flagged startup context memories."

    async def endpoint(
        self,
        reader_id: str = "default",
        query: str = "",
        preset: str = "knowledge",
        limit: int = 5,
        recent_limit: int = 5,
        namespace: str = "",
        workspace: str = "",
    ) -> str:
        """Read non-flagged startup context memories (recent + relevant)."""
        normalized_reader_id = _normalize_reader_id(reader_id)
        backend = self.get_backend()
        effective_workspace = (workspace or namespace or "").strip()
        selection_key, _ = bootstrap_selection(effective_workspace)
        flagged_version = await backend.fetch_flagged_memory_bootstrap_version(
            workspace=effective_workspace or None
        )
        if not _has_recent_flagged_bootstrap_read(
            normalized_reader_id,
            flagged_version,
            workspace=effective_workspace or None,
        ):
            return self.render_json(
                {
                    "ok": False,
                    "tool": self.operation,
                    "error": {
                        "message": (
                            "Context bootstrap required. "
                            "Call read_flagged_memories("
                            f"reader_id='{normalized_reader_id}', "
                            f"workspace='{effective_workspace}') first. "
                            f"(current_flagged_version={flagged_version})"
                        )
                    },
                }
            )
        query_text = (query or "").strip()
        relevant_rows: list[dict[str, object]] = []
        if query_text:
            try:
                relevant = await backend.recall(
                    query_text,
                    preset=preset,
                    limit=limit,
                    include_session=True,
                    wait_for_pending=True,
                    namespace=namespace or None,
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
            for scored in relevant.get("results", []):
                raw_row = await backend.fetch_memory_by_uuid(str(scored.get("uuid") or ""))
                if raw_row is not None and bool(raw_row.get("user_flagged", False)):
                    continue
                relevant_rows.append(
                    {
                        "uuid": scored.get("uuid"),
                        "name": scored.get("name"),
                        "scope": scored.get("scope"),
                        "type": scored.get("memory_type"),
                        "content": scored.get("content"),
                        "similarity": (scored.get("breakdown") or {}).get("semantic_similarity"),
                        "stale_anchor_info": scored.get("stale_anchor_info"),
                    }
                )
        recent_rows = await backend.fetch_recent_memories(
            limit=recent_limit * 3,
            namespace=namespace or None,
        )
        seen_uuids = {str(row.get("uuid") or "") for row in relevant_rows}
        non_flagged_recent: list[dict[str, object]] = []
        for row in recent_rows:
            uuid = str(row.get("uuid") or "")
            if (
                not uuid
                or bool(row.get("user_flagged", False))
                or uuid in seen_uuids
                or is_structural_memory_row(row)
            ):
                continue
            non_flagged_recent.append(row)
            if len(non_flagged_recent) >= recent_limit:
                break
        # Check for stale todos and include a warning if any exist
        stale_warning = ""
        try:
            todos = await backend.list_todos(status="open", limit=200)
            stale_count = sum(1 for t in todos if t.get("stale"))
            if stale_count:
                stale_warning = f"\n⚠️ {stale_count} open todo(s) are >30 days old. Run list_todos to review."
        except Exception:
            pass  # non-critical; don't block bootstrap

        return self.render_recall_json(
            {
                "reader_id": normalized_reader_id,
                "bootstrap_selection": selection_key,
                "bootstrap_verified": True,
                "flagged_version": flagged_version,
                "query": query_text or None,
                "preset": preset if query_text else None,
                "relevant_count": len(relevant_rows),
                "recent_count": len(non_flagged_recent),
                "relevant": [_compact_memory_item(row, tag="relevant") for row in relevant_rows],
                "recent": [_compact_memory_item(row, tag="recent") for row in non_flagged_recent],
                "stale_todo_warning": stale_warning or None,
            }
        )
