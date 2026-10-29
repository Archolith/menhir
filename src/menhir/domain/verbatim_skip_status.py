"""Effective enrichment status of a client episode finished by verbatim skip.

A verbatim skip marks the client READY without extraction while its evidence projection,
which extracts the same text, may still be PENDING or ENRICHING. The client row's READY then
means "never extract this row", not "this memory is extracted". Paths that REPORT or WAIT on
an episode's enrichment read the projection's state through ``effective_processing_row``.
Work fences (lease ownership, enqueue/requeue checks, operator lease release) keep reading the
raw row: a READY client must never look ENRICHING to a fence.
"""

from __future__ import annotations

from typing import Any, Mapping

from menhir.domain.models import ProcessingState

VERBATIM_SKIPPED_SUBSTAGE = "verbatim_skipped"
#: Reported while a skipped client's projection is FAILED or gone and
#: ``reenable_verbatim_skips`` has not yet returned the client to PENDING.
PROJECTION_LOST_SUBSTAGE = "verbatim_skip_projection_lost"

#: Projection fields the status read returns alongside the client row (``skip_projection``).
SKIP_PROJECTION_FIELDS = (
    "uuid",
    "processing_state",
    "processing_stage",
    "processing_substage",
    "processing_substage_started_at",
    "processing_progress",
    "processing_steps_total",
    "processing_steps_completed",
    "processing_llm_tasks_attempt",
    "processing_llm_tasks_total",
    "processing_llm_last_task_at",
    "processing_llm_active_task",
    "processing_llm_active_kind",
    "processing_llm_active_model",
    "processing_llm_active_endpoint",
    "processing_heartbeat_at",
    "processing_started_at",
    "processing_completed_at",
    "processing_error",
    "resolved_episode_uuid",
    "enriched_nodes_touched",
    "enriched_edges_touched",
)

# Progress fields copied from an unfinished projection. Owner, lease and attempts stay the
# client's own: they describe the client row, and nothing may act on them as the projection's.
_IN_FLIGHT_FIELDS = (
    "processing_state",
    "processing_stage",
    "processing_substage",
    "processing_substage_started_at",
    "processing_progress",
    "processing_steps_total",
    "processing_steps_completed",
    "processing_llm_tasks_attempt",
    "processing_llm_tasks_total",
    "processing_llm_last_task_at",
    "processing_llm_active_task",
    "processing_llm_active_kind",
    "processing_llm_active_model",
    "processing_llm_active_endpoint",
    "processing_heartbeat_at",
    "processing_started_at",
    "processing_error",
)


def is_verbatim_skipped(row: Mapping[str, Any]) -> bool:
    return (
        row.get("processing_state") == ProcessingState.READY
        and row.get("processing_substage") == VERBATIM_SKIPPED_SUBSTAGE
        and bool(row.get("extraction_skipped_for"))
    )


def effective_processing_row(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return the row a status reader should see; rows that are not skips come back unchanged.

    - projection READY: the client stays READY (substage ``verbatim_skipped``) and carries the
      projection's enriched episode, touch counts and completion time.
    - projection PENDING/ENRICHING: the client reports the projection's progress, so a wait
      keeps waiting until the extraction that covers it is done.
    - projection FAILED or gone: the client reports PENDING (``verbatim_skip_projection_lost``);
      ``reenable_verbatim_skips`` will queue it for its own extraction.
    """
    if row is None or not is_verbatim_skipped(row):
        return row
    projection = row.get("skip_projection")
    out = dict(row)
    out["extraction_projection_uuid"] = row.get("extraction_skipped_for")
    state = projection.get("processing_state") if isinstance(projection, Mapping) else None
    if state == ProcessingState.READY:
        if not out.get("resolved_episode_uuid"):
            out["resolved_episode_uuid"] = projection.get("resolved_episode_uuid")
        out["enriched_nodes_touched"] = projection.get("enriched_nodes_touched")
        out["enriched_edges_touched"] = projection.get("enriched_edges_touched")
        out["processing_completed_at"] = projection.get("processing_completed_at")
        return out
    if state in (ProcessingState.PENDING, ProcessingState.ENRICHING):
        for field in _IN_FLIGHT_FIELDS:
            out[field] = projection.get(field)
        out["processing_completed_at"] = None
        return out
    out.update(
        processing_state=ProcessingState.PENDING.value,
        processing_stage="queued",
        processing_substage=PROJECTION_LOST_SUBSTAGE,
        processing_progress=0.0,
        processing_steps_completed=0,
        processing_completed_at=None,
        processing_error=None,
    )
    return out
