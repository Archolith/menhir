"""Scheduler conflict-maintenance task functions.

Extracted verbatim from ``scheduler_tasks.py``; the facade module re-exports everything
here so existing ``menhir.services.scheduler_tasks`` import sites keep working unchanged.
"""

from __future__ import annotations

import asyncio
import logging

from menhir.services.scheduler_protocols import SchedulerLifecycleService

#: Same logger object/name as the facade module: every record these tasks emit must keep
#: the ``menhir.services.scheduler_tasks`` logger name (caplog filters key off it).
logger = logging.getLogger("menhir.services.scheduler_tasks")


# ---------------------------------------------------------------------------
# Job: auto-resolve stale conflicts
# ---------------------------------------------------------------------------

async def auto_resolve_conflicts(
    lifecycle_service: SchedulerLifecycleService,
    *,
    max_age_days: int = 14,
    limit: int = 50,
) -> dict[str, object]:
    groups_resolved = await asyncio.to_thread(
        lifecycle_service.auto_resolve_stale_conflicts,
        max_age_days=max_age_days,
        limit=limit,
    )
    return {"groups_resolved": groups_resolved}


# ---------------------------------------------------------------------------
# Job: confirm pending conflicts via LLM
# ---------------------------------------------------------------------------

async def confirm_conflicts(
    lifecycle_service: SchedulerLifecycleService,
    *,
    limit: int = 20,
) -> dict[str, object]:
    counts = await lifecycle_service.confirm_pending_conflicts(limit=limit)
    return dict(counts)


# ---------------------------------------------------------------------------
# Job: LLM-review unresolved conflicts (weekly, clears false positives)
# ---------------------------------------------------------------------------

async def review_unresolved_conflicts(
    lifecycle_service: SchedulerLifecycleService,
    *,
    limit: int = 50,
) -> dict[str, object]:
    """Re-evaluate unresolved conflict groups through the LLM reviewer.

    Groups that the LLM clears as false positives are resolved as keep_both.
    Groups that the LLM confirms as genuine contradictions stay unresolved
    for manual attention. This prevents the unresolved queue from growing
    indefinitely with pairs that are merely similar but not contradictory.
    """
    counts = await lifecycle_service.confirm_pending_conflicts(
        limit=limit, status="unresolved", verbose=True,
    )
    logger.info(
        "review_unresolved_conflicts: confirmed=%s cleared=%s errors=%s",
        counts.get("confirmed"), counts.get("cleared"), counts.get("errors"),
    )
    return dict(counts)
