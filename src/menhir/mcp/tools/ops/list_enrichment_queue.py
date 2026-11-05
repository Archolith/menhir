"""MCP tool: list_enrichment_queue."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Mapping

from menhir.domain.models import ProcessingState
from menhir.domain.verbatim_skip_status import PROJECTION_LOST_SUBSTAGE, effective_processing_row
from menhir.mcp.formatters import _coerce_iso, _resolve_queue_state_filter, _stale_reason_for_row
from menhir.mcp.tools.base import BaseTextTool
from menhir.mcp.contracts import ToolScope


async def list_enrichment_queue(
    state: str = "active", limit: int = 25, namespace: str = ""
) -> str:
    """List episodic enrichment queue rows with stale-state hints.

    Args:
        state: Queue state filter ("active", "all", "pending", "enriching", "ready", "failed").
        limit: Max rows to return (default: 25, max: 200).

    Returns:
        Queue summary and row-level diagnostics.
    """

    return await ListEnrichmentQueueTool().execute(
        state=state, limit=limit, namespace=namespace
    )


def _reported_row(row: dict) -> dict:
    """The row a reader should see: a verbatim-skipped client reports its extraction.

    While the extraction is in flight the owner, lease and attempts shown are the projection's,
    so they describe the same row as the state and the stale hint (display only: nothing acts on
    it) judges the lease that is actually held.
    """

    effective = effective_processing_row(row)
    if effective is None or effective is row:
        return row
    projection = row.get("skip_projection")
    in_flight = effective.get("processing_state") in (
        ProcessingState.PENDING, ProcessingState.ENRICHING,
    ) and effective.get("processing_substage") != PROJECTION_LOST_SUBSTAGE
    if in_flight and isinstance(projection, Mapping):
        effective = {
            **effective,
            **{
                key: projection.get(key)
                for key in ("processing_owner", "processing_lease_expires_at", "processing_attempts")
            },
        }
    return effective


class ListEnrichmentQueueTool(BaseTextTool):
    name = "list_enrichment_queue"
    # NAMESPACED: the rows carry session_id, source and enrichment error text -- tenant-
    # identifying operational metadata, reaching every silo at readonly tier with no predicate.
    # Milder than a content read, which is why it is filed below `list_conflicts`, not beside it.
    scope = ToolScope.NAMESPACED
    required_tier = "readonly"
    description = "List episodic enrichment queue rows with stale-state hints."
    title = "List Enrichment Queue"
    oauth_scopes = ("menhir:read",)
    read_only_hint = True
    destructive_hint = False
    open_world_hint = False

    async def endpoint(
        self, state: str = "active", limit: int = 25, namespace: str = ""
    ) -> str:
        backend = self.get_backend()
        state_label, state_filter = _resolve_queue_state_filter(state)
        scope = {"namespace": namespace} if namespace else {}
        rows = await backend.list_episode_processing(
            states=state_filter,
            limit=limit,
            **scope,
        )
        now_utc = datetime.now(timezone.utc)

        lines = [
            f"filter: {state_label}",
            # The listing is the queue: it filters on each row's OWN state. A verbatim-skipped
            # episode is READY there and reports its extraction's state in `state=`, with the
            # queue state alongside as `queue_state=`.
            "filter_on: queue_state",
            f"count: {len(rows)}",
            f"in_memory_queue_depth: {await backend.get_queue_depth()}",
        ]

        if not rows:
            lines.append("rows: (none)")
            return "\n".join(lines)

        lines.append("rows:")
        for index, row in enumerate(rows, 1):
            queue_state = row.get("processing_state")
            row = _reported_row(row)
            stale_reason = _stale_reason_for_row(row, now_utc=now_utc)
            projected_note = ""
            if row.get("processing_state") != queue_state:
                projected_note = (
                    f"queue_state={queue_state} "
                    f"extraction_projection={row.get('extraction_projection_uuid')} "
                )
            lines.append(
                f"  [{index}] uuid={row.get('uuid')} state={row.get('processing_state') or 'UNKNOWN'} "
                f"{projected_note}"
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
