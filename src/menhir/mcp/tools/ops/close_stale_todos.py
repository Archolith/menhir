"""MCP tool: close_stale_todos."""

from __future__ import annotations

from menhir.mcp.tools.base import BaseTextTool
from menhir.mcp.contracts import ToolScope


async def close_stale_todos(
    older_than_days: int = 60, dry_run: bool = True, namespace: str = ""
) -> str:
    """Close stale TODO items that have been open for too long.

    Args:
        older_than_days: Close todos open for more than this many days (default: 60, max: 365).
        dry_run: Preview only -- don't actually close any todos (default: true).
        namespace: Restrict to a single silo. A pinned client has this forced.

    Returns:
        Summary of closed or previewed stale todos.
    """
    return await CloseStaleTodosTool().execute(
        older_than_days=older_than_days, dry_run=dry_run, namespace=namespace
    )


class CloseStaleTodosTool(BaseTextTool):
    name = "close_stale_todos"
    scope = ToolScope.NAMESPACED
    title = "Close Stale TODOs"
    oauth_scopes = ("menhir:write",)
    read_only_hint = False
    destructive_hint = False
    open_world_hint = False
    description = "Close stale TODO items that have been open for too long."

    async def endpoint(
        self, older_than_days: int = 60, dry_run: bool = True, namespace: str = ""
    ) -> str:
        """Close stale TODO items that have been open for too long.

        Args:
            older_than_days: Close todos open for more than this many days (default 60, max 365).
            dry_run: Preview only -- don't actually close any todos (default true).
            namespace: Restrict to a single silo. A pinned client has this forced -- and the
                parameter existing is what makes that possible, since the pin is injected only
                into endpoints whose signature declares it.
        """
        backend = self.get_backend()
        result = await backend.close_stale_todos(
            older_than_days=max(1, min(older_than_days, 365)),
            dry_run=dry_run,
            namespace=namespace.strip() or None,
        )

        lines: list[str] = []
        if dry_run:
            lines.append(f"[DRY RUN] Stale todos older than {result.get('older_than_days', 60)} days:")
            lines.append(f"  Would close: {result.get('preview_count', 0)}")
        else:
            lines.append(f"Closed {result.get('closed_count', 0)} stale todos (older than {result.get('older_than_days', 60)} days)")

        preview = result.get("preview", [])
        if preview:
            lines.append("  UUIDs:")
            for uuid in preview[:10]:
                lines.append(f"    - {uuid}")
            if len(preview) > 10:
                lines.append(f"    ... and {len(preview) - 10} more")

        return "\n".join(lines)
