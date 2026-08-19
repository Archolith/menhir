"""MCP tool: recover_orphans."""

from __future__ import annotations

from datetime import datetime, timezone

from menhir.core.backend_impl import RuntimeProvider
from menhir.infrastructure.scheduler_trace import emit_scheduler_task_event
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

        total = len(candidates)
        job_id = f"orphan-recovery-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
        await emit_scheduler_task_event(
            parent_job_id=job_id,
            parent_label=f"orphan recovery (>{max_age_hours}h)",
            parent_state="running",
            parent_heartbeat_at=datetime.now(timezone.utc).isoformat(),
            parent_metadata={"processed": 0, "total": total},
        )

        async def _on_progress(processed: int, total: int, current_node: str) -> None:
            await emit_scheduler_task_event(
                parent_job_id=job_id,
                parent_label=f"orphan recovery (>{max_age_hours}h)",
                parent_state="running",
                parent_heartbeat_at=datetime.now(timezone.utc).isoformat(),
                parent_metadata={"processed": processed, "total": total, "current_node": current_node},
            )

        try:
            if isinstance(backend, RuntimeProvider):
                result = await backend.built.lifecycle_service.recover_orphans(
                    max_age_hours=max_age_hours,
                    on_progress=_on_progress,
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
            await emit_scheduler_task_event(
                parent_job_id=job_id,
                parent_label=f"orphan recovery (>{max_age_hours}h)",
                parent_state="ready",
                parent_heartbeat_at=datetime.now(timezone.utc).isoformat(),
                parent_metadata={"processed": total, "total": total, **summary},
            )
            return self.render_json(summary)
        except Exception as exc:
            await emit_scheduler_task_event(
                parent_job_id=job_id,
                parent_label=f"orphan recovery (>{max_age_hours}h)",
                parent_state="failed",
                parent_error=str(exc),
                parent_heartbeat_at=datetime.now(timezone.utc).isoformat(),
            )
            raise
