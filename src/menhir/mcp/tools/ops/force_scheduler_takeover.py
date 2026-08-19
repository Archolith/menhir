"""MCP tool: force_scheduler_takeover."""

from __future__ import annotations

from menhir.mcp.tools.base import BaseTextTool
from menhir.mcp.contracts import ToolScope


async def force_scheduler_takeover(reason: str = "manual-troubleshooting") -> str:
    """Force this MCP process to take scheduler lease ownership for troubleshooting.

    Args:
        reason: Short operator reason for audit context.

    Returns:
        Scheduler lease status after takeover attempt.
    """

    return await ForceSchedulerTakeoverTool().execute(reason=reason)


class ForceSchedulerTakeoverTool(BaseTextTool):
    name = "force_scheduler_takeover"
    scope = ToolScope.GLOBAL
    required_tier = "operator"
    description = "Force this MCP process to take scheduler lease ownership."

    async def endpoint(self, reason: str = "manual-troubleshooting") -> str:
        backend = self.get_backend()
        forced = await backend.scheduler_force_takeover(reason=reason)
        snapshot = await backend.scheduler_status_snapshot() or {}
        lease = snapshot.get("lease") or {}
        active_owner = lease.get("active_owner") or {}
        lines = [
            f"force_requested: {reason}",
            f"takeover_applied: {forced}",
            f"scheduler_running: {snapshot.get('running')}",
            f"lease_acquired: {lease.get('acquired')}",
            f"lease_blocked_reason: {lease.get('blocked_reason')}",
            f"active_owner_pid: {active_owner.get('owner_pid')}",
            f"active_owner_id: {active_owner.get('owner_id')}",
            f"active_owner_expired: {active_owner.get('expired')}",
        ]
        forced_meta = lease.get("last_forced_takeover") or {}
        if forced_meta:
            lines.append(f"last_forced_takeover_at: {forced_meta.get('at')}")
            lines.append(f"last_forced_takeover_reason: {forced_meta.get('reason')}")
        return "\n".join(lines)
