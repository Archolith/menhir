"""MCP tool: list_todos."""

from __future__ import annotations

from menhir.mcp.tools.base import BaseTextTool


async def list_todos(status: str = "open", limit: int = 25, namespace: str = "") -> str:
    """List TODO items, optionally filtered by status.

    Args:
        status: "open" (default) or "closed".
        limit: Max rows to return (default 25, max 200).
        namespace: Optional silo to scope to. Empty lists every silo. Supplying
                   one narrows to that silo plus the shared "default" bucket.

    Returns:
        Formatted list of TODOs with uuid, priority, code ref, and content.
    """
    return await ListTodosTool().execute(status=status, limit=limit, namespace=namespace)


class ListTodosTool(BaseTextTool):
    name = "list_todos"
    required_tier = "readonly"
    description = "List persistent TODO items filtered by status."

    async def endpoint(self, status: str = "open", limit: int = 25, namespace: str = "") -> str:
        backend = self.get_backend()
        rows = await backend.list_todos(
            status=status, limit=min(limit, 200), namespace=namespace or None
        )

        scope = f" in {namespace}+default" if namespace else ""
        lines = [f"{status} ({len(rows)}){scope}"]
        if not rows:
            lines.append("(none)")
            return "\n".join(lines)

        truncated_any = False
        stale_count = sum(1 for r in rows if r.get("stale"))
        if stale_count:
            lines.append(f"⚠ {stale_count} todo(s) older than 30 days — consider closing or splitting")

        for row in rows:
            tag = (row.get("priority") or "normal").upper()
            uuid = row.get("uuid", "?")
            ref = f"  {row['code_ref']}" if row.get("code_ref") else ""
            content = row.get("content", "")
            truncated = len(content) > 100
            snippet = (content[:100] + "...") if truncated else content
            truncated_any = truncated_any or truncated
            created = (row.get("created_at") or "")[:10]
            closed = (row.get("closed_at") or "")[:10] or "-"
            due = f" | due: {row['due_date']}" if row.get("due_date") else ""
            age = row.get("age_days")
            stale_marker = " ⚠ STALE" if row.get("stale") else ""
            age_str = f" | age: {age}d" if age is not None else ""
            lines.append(f"[{tag}] uuid={uuid}{ref} — {snippet}{stale_marker}")
            lines.append(f"        created: {created} | closed: {closed}{due}{age_str}")

        if truncated_any:
            lines.append("… content above is truncated at 100 chars — get_todo(uuid) returns the full text")

        return "\n".join(lines)
