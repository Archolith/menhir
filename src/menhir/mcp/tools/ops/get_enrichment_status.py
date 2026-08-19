"""MCP tool: get_enrichment_status."""

from __future__ import annotations

from menhir.mcp.formatters import (
    _coerce_iso,
    _collect_episode_status,
    _format_episode_status,
    _require_episode_uuid,
)
from menhir.mcp.tools.base import BaseTextTool
from menhir.mcp.contracts import ToolScope


async def get_enrichment_status(
    episode_uuid: str,
    wait: bool = False,
    timeout_s: float = 30.0,
    poll_interval_s: float = 1.0,
) -> str:
    """Inspect one episode's enrichment status, with optional wait for completion.

    Args:
        episode_uuid: Episode UUID to inspect.
        wait: If true, poll until READY/FAILED/timeout.
        timeout_s: Max wait time when wait=true.
        poll_interval_s: Poll interval when wait=true.

    Returns:
        Episode status summary with observed state updates.
    """

    return await GetEnrichmentStatusTool().execute(
        episode_uuid=episode_uuid,
        wait=wait,
        timeout_s=timeout_s,
        poll_interval_s=poll_interval_s,
    )


class GetEnrichmentStatusTool(BaseTextTool):
    name = "get_enrichment_status"
    scope = ToolScope.OBJECT
    required_tier = "readonly"
    description = "Inspect one episode's enrichment status."

    def timeout_for(
        self,
        episode_uuid: str,
        wait: bool = False,
        timeout_s: float = 30.0,
        poll_interval_s: float = 1.0,
    ) -> int:
        return max(30, int(timeout_s) + 5) if wait else 30

    async def endpoint(
        self,
        episode_uuid: str,
        wait: bool = False,
        timeout_s: float = 30.0,
        poll_interval_s: float = 1.0,
    ) -> str:
        backend = self.get_backend()
        normalized_uuid = _require_episode_uuid(episode_uuid)
        if wait:
            row, history, timed_out = await _collect_episode_status(
                backend,
                normalized_uuid,
                timeout_s=timeout_s,
                poll_interval_s=poll_interval_s,
            )
            return _format_episode_status(
                episode_uuid=normalized_uuid,
                row=row,
                history=history,
                timed_out=timed_out,
            )

        row = await backend.fetch_episode_processing(normalized_uuid)
        history = []
        if row is not None:
            history.append(
                {
                    "state": str(row.get("processing_state") or "UNKNOWN"),
                    "stage": str(row.get("processing_stage") or ""),
                    "substage": str(row.get("processing_substage") or ""),
                    "substage_started_at": _coerce_iso(row.get("processing_substage_started_at")),
                    "progress": float(row.get("processing_progress") or 0.0),
                    "steps_total": int(row.get("processing_steps_total") or 0),
                    "steps_completed": int(row.get("processing_steps_completed") or 0),
                    "llm_tasks_attempt": int(row.get("processing_llm_tasks_attempt") or 0),
                    "llm_tasks_total": int(row.get("processing_llm_tasks_total") or 0),
                    "attempts": int(row.get("processing_attempts") or 0),
                    "queue_depth": await backend.get_queue_depth(),
                    "processing_error": row.get("processing_error"),
                    "llm_active_task": row.get("processing_llm_active_task"),
                    "llm_active_kind": row.get("processing_llm_active_kind"),
                    "llm_active_model": row.get("processing_llm_active_model"),
                    "llm_active_endpoint": row.get("processing_llm_active_endpoint"),
                    "llm_last_task_at": _coerce_iso(row.get("processing_llm_last_task_at")),
                    "heartbeat_at": _coerce_iso(row.get("processing_heartbeat_at")),
                    "started_at": _coerce_iso(row.get("processing_started_at")),
                    "completed_at": _coerce_iso(row.get("processing_completed_at")),
                }
            )
        return _format_episode_status(
            episode_uuid=normalized_uuid,
            row=row,
            history=history,
            timed_out=False,
        )
