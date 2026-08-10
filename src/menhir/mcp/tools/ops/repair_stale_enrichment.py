"""MCP tool: repair_stale_enrichment."""

from __future__ import annotations

from menhir.mcp.formatters import _coerce_iso
from menhir.mcp.tools.base import BaseTextTool


async def repair_stale_enrichment(dry_run: bool = True, limit: int = 100) -> str:
    """Inspect and optionally repair stale ENRICHING episodes.

    Args:
        dry_run: If true, report stale episodes without modifying state.
        limit: Max stale rows to inspect (default: 100, max: 500).

    Returns:
        Repair summary and stale episode details.
    """

    return await RepairStaleEnrichmentTool().execute(dry_run=dry_run, limit=limit)


class RepairStaleEnrichmentTool(BaseTextTool):
    name = "repair_stale_enrichment"
    required_tier = "operator"
    description = "Inspect and optionally repair stale ENRICHING episodes."

    async def endpoint(self, dry_run: bool = True, limit: int = 100) -> str:
        backend = self.get_backend()
        stale_rows = await backend.fetch_stale_enriching_episodes(limit=limit)
        lines = [
            f"dry_run: {dry_run}",
            f"stale_detected: {len(stale_rows)}",
        ]

        if dry_run:
            if not stale_rows:
                lines.append("rows: (none)")
                return "\n".join(lines)
            lines.append("rows:")
            for index, row in enumerate(stale_rows, 1):
                lines.append(
                    f"  [{index}] uuid={row.get('uuid')} attempts={int(row.get('processing_attempts') or 0)} "
                    f"stage={row.get('processing_stage') or '(none)'} "
                    f"progress={float(row.get('processing_progress') or 0.0):.1f} "
                    f"steps={int(row.get('processing_steps_completed') or 0)}/{int(row.get('processing_steps_total') or 0)} "
                    f"llm_tasks={int(row.get('processing_llm_tasks_attempt') or 0)} "
                    f"owner={row.get('processing_owner') or '(none)'} "
                    f"lease_expires_at={_coerce_iso(row.get('processing_lease_expires_at')) or '(none)'} "
                    f"llm_last_task_at={_coerce_iso(row.get('processing_llm_last_task_at')) or '(none)'} "
                    f"heartbeat_at={_coerce_iso(row.get('processing_heartbeat_at')) or '(none)'} "
                    f"started_at={_coerce_iso(row.get('processing_started_at')) or '(none)'} "
                    f"error={row.get('processing_error') or '(none)'}"
                )
            return "\n".join(lines)

        recovery = await backend.recover_stale_enrichment_leases(limit=limit)
        remaining = await backend.fetch_stale_enriching_episodes(limit=limit)
        lines.extend(
            [
                f"stale_reset: {int(recovery.get('stale_reset') or 0)}",
                f"requeued_pending: {int(recovery.get('requeued_pending') or 0)}",
                f"stale_remaining: {len(remaining)}",
                f"in_memory_queue_depth: {await backend.get_queue_depth()}",
            ]
        )
        return "\n".join(lines)
