"""MCP tool: resume_scheduler."""

from __future__ import annotations

from menhir.mcp.tools.base import BaseTextTool
from menhir.mcp.contracts import ToolScope


async def resume_scheduler() -> str:
    """Restart the maintenance scheduler loop after a pause.

    Attempts to re-acquire the scheduler lease and restart background jobs
    (enrichment retries, stale-lease recovery, conflict jobs, structure watcher).

    Returns:
        Scheduler state after resume attempt.
    """

    return await ResumeSchedulerTool().execute()


class ResumeSchedulerTool(BaseTextTool):
    name = "resume_scheduler"
    scope = ToolScope.GLOBAL
    required_tier = "operator"
    description = "Restart the maintenance scheduler loop after a pause."

    async def endpoint(self) -> str:
        backend = self.get_backend()
        started = await backend.scheduler_resume()
        snapshot = await backend.scheduler_status_snapshot() or {}
        lease = snapshot.get("lease") or {}
        lines = [
            f"start_succeeded: {started}",
            f"scheduler_running: {snapshot.get('running')}",
            f"lease_acquired: {lease.get('acquired')}",
            f"lease_blocked_reason: {lease.get('blocked_reason')}",
        ]
        return "\n".join(lines)
