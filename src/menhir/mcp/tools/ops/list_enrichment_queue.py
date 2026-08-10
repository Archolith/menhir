"""MCP tool: list_enrichment_queue."""

from __future__ import annotations

from datetime import datetime, timezone

from menhir.mcp.formatters import _coerce_iso, _resolve_queue_state_filter, _stale_reason_for_row
from menhir.mcp.tools.base import BaseTextTool


async def list_enrichment_queue(state: str = "active", limit: int = 25) -> str:
    """List episodic enrichment queue rows with stale-state hints.

    Args:
        state: Queue state filter ("active", "all", "pending", "enriching", "ready", "failed").
        limit: Max rows to return (default: 25, max: 200).

    Returns:
        Queue summary and row-level diagnostics.
    """

    return await ListEnrichmentQueueTool().execute(state=state, limit=limit)


class ListEnrichmentQueueTool(BaseTextTool):
    name = "list_enrichment_queue"
    required_tier = "readonly"
    description = "List episodic enrichment queue rows with stale-state hints."

    async def endpoint(self, state: str = "active", limit: int = 25) -> str:
        backend = self.get_backend()
        state_label, state_filter = _resolve_queue_state_filter(state)
        rows = await backend.list_episode_processing(
            states=state_filter,
            limit=limit,
        )
        now_utc = datetime.now(timezone.utc)

        lines = [
            f"filter: {state_label}",
            f"count: {len(rows)}",
            f"in_memory_queue_depth: {await backend.get_queue_depth()}",
        ]

        if not rows:
            lines.append("rows: (none)")
            return "\n".join(lines)

        lines.append("rows:")
        for index, row in enumerate(rows, 1):
            stale_reason = _stale_reason_for_row(row, now_utc=now_utc)
            lines.append(
                f"  [{index}] uuid={row.get('uuid')} state={row.get('processing_state') or 'UNKNOWN'} "
                f"stage={row.get('processing_stage') or '(none)'} "
                f"progress={float(row.get('processing_progress') or 0.0):.1f} "
                f"steps={int(row.get('processing_steps_completed') or 0)}/{int(row.get('processing_steps_total') or 0)} "
                f"llm_tasks={int(row.get('processing_llm_tasks_attempt') or 0)} "
                f"attempts={int(row.get('processing_attempts') or 0)} stale={stale_reason or 'no'} "
                f"owner={row.get('processing_owner') or '(none)'} "
                f"lease_expires_at={_coerce_iso(row.get('processing_lease_expires_at')) or '(none)'} "
                f"llm_last_task_at={_coerce_iso(row.get('processing_llm_last_task_at')) or '(none)'} "
                f"heartbeat_at={_coerce_iso(row.get('processing_heartbeat_at')) or '(none)'} "
                f"started_at={_coerce_iso(row.get('processing_started_at')) or '(none)'} "
                f"error={row.get('processing_error') or '(none)'}"
            )
        return "\n".join(lines)
