"""MCP tool: recover_orphans."""

from __future__ import annotations


from menhir.mcp.tools.base import BaseJsonTool
from menhir.mcp.contracts import ToolScope


async def recover_orphans(max_age_hours: float = 4.0, dry_run: bool = False) -> str:
    """Recover orphaned SESSION nodes from crashed or abandoned sessions.

    Promotes or deletes stale SESSION nodes that were never consolidated.
    Run this when the MCP server skipped orphan recovery during init,
    or to manually clean up accumulated SESSION nodes.

    Args:
        max_age_hours: Consolidate SESSION nodes older than this (default: 4.0).
            Expired demotion TTLs and empty orphan episodes use separate age rules.
        dry_run: If true, report counts for all recovery phases without making changes.

    Returns:
        Preview counts or a summary of promoted, demoted, deleted, and skipped nodes.
    """

    return await RecoverOrphansTool().execute(max_age_hours=max_age_hours, dry_run=dry_run)


class RecoverOrphansTool(BaseJsonTool):
    name = "recover_orphans"
    scope = ToolScope.GLOBAL
    required_tier = "operator"
    description = "Recover global stale SESSION nodes and run expired TTL and empty-episode cleanup."
    title = "Recover Orphaned Sessions"
    oauth_scopes = ("menhir:admin",)
    read_only_hint = False
    destructive_hint = True
    open_world_hint = False

    def timeout_for(self, max_age_hours: float = 4.0, dry_run: bool = False) -> int:
        return 900

    async def endpoint(self, max_age_hours: float = 4.0, dry_run: bool = False) -> str:
        backend = self.get_backend()
        summary = await backend.recover_orphans(max_age_hours=max_age_hours, dry_run=dry_run)
        return self.render_json(summary)
