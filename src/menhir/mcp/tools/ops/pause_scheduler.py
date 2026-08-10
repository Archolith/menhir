"""MCP tool: pause_scheduler."""

from __future__ import annotations

from menhir.mcp.tools.base import BaseTextTool


async def pause_scheduler() -> str:
    """Stop the maintenance scheduler loop without restarting the process.

    Pausing prevents background enrichment retries, stale-lease recovery, and
    conflict jobs from running. Use resume_scheduler to restart the loop.
    In-flight enrichment already leased by a worker will continue to completion.

    Returns:
        Scheduler state after pause.
    """

    return await PauseSchedulerTool().execute()


class PauseSchedulerTool(BaseTextTool):
    name = "pause_scheduler"
    required_tier = "operator"
    description = "Stop the maintenance scheduler loop (enrichment retries, stale recovery, conflict jobs)."

    async def endpoint(self) -> str:
        backend = self.get_backend()
        was_running = await backend.scheduler_pause()
        snapshot = await backend.scheduler_status_snapshot() or {}
        lines = [
            f"was_running: {was_running}",
            f"scheduler_running: {snapshot.get('running')}",
        ]
        lease = snapshot.get("lease") or {}
        lines.append(f"lease_acquired: {lease.get('acquired')}")
        return "\n".join(lines)
