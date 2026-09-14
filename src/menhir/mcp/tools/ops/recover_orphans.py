"""MCP tool: recover_orphans."""

from __future__ import annotations


from menhir.core.backend_impl import RuntimeProvider
from menhir.mcp.tools.base import BaseJsonTool
from menhir.mcp.contracts import ToolScope


async def recover_orphans(max_age_hours: float = 4.0, dry_run: bool = False) -> str:
    """Recover orphaned SESSION nodes from crashed or abandoned sessions.

    Promotes or deletes stale SESSION nodes that were never consolidated.
    Run this when the MCP server skipped orphan recovery during init,
    or to manually clean up accumulated SESSION nodes.

    Args:
        max_age_hours: Only process SESSION nodes older than this (default: 4.0).
        dry_run: If true, report counts without making changes.

    Returns:
        Summary of promoted, deleted, and skipped nodes.
    """

    return await RecoverOrphansTool().execute(max_age_hours=max_age_hours, dry_run=dry_run)


class RecoverOrphansTool(BaseJsonTool):
    name = "recover_orphans"
    scope = ToolScope.GLOBAL
    required_tier = "operator"
    description = "Recover orphaned SESSION nodes from crashed sessions."
    title = "Recover Orphaned Sessions"
    oauth_scopes = ("menhir:admin",)
    read_only_hint = False
    destructive_hint = True
    open_world_hint = False

    def timeout_for(self, max_age_hours: float = 4.0, dry_run: bool = False) -> int:
        return 900

    async def endpoint(self, max_age_hours: float = 4.0, dry_run: bool = False) -> str:
        backend = self.get_backend()
        candidates = await backend.fetch_session_entities(
            session_id=None,
            max_age_hours=max_age_hours,
        )

        if dry_run:
            return self.render_json(
                {
                    "dry_run": True,
                    "session_nodes_found": len(candidates),
                    "max_age_hours": max_age_hours,
                }
            )

        if isinstance(backend, RuntimeProvider):
            result = await backend.built.lifecycle_service.recover_orphans(
                max_age_hours=max_age_hours,
            )
            summary = {
                "promoted": result.promoted,
                "deleted": result.deleted,
                "conflicts_detected": result.conflicts_detected,
                "skipped_pending": result.skipped_pending,
                "orphan_episodes_cleaned": result.orphan_episodes_cleaned,
            }
        else:
            summary = await backend.recover_orphans(max_age_hours=max_age_hours)
        return self.render_json(summary)
