"""Scheduler queue-health task function.

Extracted verbatim from ``scheduler_tasks.py``; the facade module re-exports everything
here so existing ``menhir.services.scheduler_tasks`` import sites keep working unchanged.
"""

from __future__ import annotations

import asyncio
import logging

from menhir.services.enrichment_failures import (
    classify_enrichment_failure,
    is_budget_refusal,
)
from menhir.services.scheduler_protocols import (
    SchedulerGraphAdapter,
    SchedulerIngestService,
)

#: Same logger object/name as the facade module: every record this task emits must keep
#: the ``menhir.services.scheduler_tasks`` logger name (caplog filters key off it).
logger = logging.getLogger("menhir.services.scheduler_tasks")


# ---------------------------------------------------------------------------
# Job: observe queue health
# ---------------------------------------------------------------------------

#: Classifications that `retry_process_candidate` refuses to requeue. Episodes carrying one
#: are done being handled automatically -- no scheduler pass will ever pick them up again.
_NEVER_RETRIED_CLASSIFICATIONS = ("terminal", "manual_review")


async def observe_queue_health(
    ingest_service: SchedulerIngestService,
    graph_adapter: SchedulerGraphAdapter,
) -> dict[str, object]:
    """Report queue counts, and WARN loudly about episodes nothing will ever retry.

    A FAILED episode classified `terminal` or `manual_review` is parked permanently: it holds
    its content but has no `:Entity` nodes, and recall searches `:Entity`, so the memory is
    invisible while `add_memory` already reported success to the caller. Nothing else in the
    system says so -- `_run_job` files this job's return value into the telemetry sidecar and
    never logs it, so a backlog here is silent by construction.

    That is exactly how 196 episodes sat unrecallable for eight months (an OpenAI 400
    context-length error fell through every marker list to the `manual_review` default, which
    is correct -- a 400 is not retryable -- but nothing surfaced the growing pile). The counts
    below are cheap; the warning is the point.
    """
    # CF-99: the maintenance loop must never block. A blocked loop cannot renew the
    # scheduler lease, which lets a second owner start mutating the same graph.
    overview = await asyncio.to_thread(graph_adapter.fetch_memory_overview)
    result: dict[str, object] = {
        "queue_depth": ingest_service.get_queue_depth(),
        "failed_enrichments": ingest_service.get_failed_enrichment_count(),
        "pending_count": overview.get("pending_count"),
        "enriching_count": overview.get("enriching_count"),
        "failed_count": overview.get("failed_count"),
    }

    try:
        signatures = await asyncio.to_thread(
            graph_adapter.fetch_failed_error_signatures, limit=25
        )
    except Exception as exc:  # noqa: BLE001 - health reporting must never break the loop
        logger.warning("queue health: could not group failed-episode errors: %s", exc)
        return result

    buckets: dict[str, int] = {}
    budget_parked = 0
    worst: tuple[int, str, str] | None = None  # (count, error, oldest_at)
    for row in signatures:
        error = str(row.get("error") or "")
        count = int(row.get("count") or 0)
        classification = classify_enrichment_failure(error)
        buckets[classification] = buckets.get(classification, 0) + count
        if is_budget_refusal(error) and classification in _NEVER_RETRIED_CLASSIFICATIONS:
            # Only refusals nothing will retry are "parked". A session-window refusal is now
            # retryable and clears itself, so counting it here would report a backlog that
            # needs no operator action -- the exact dilution this metric was split out to avoid.
            budget_parked += count
        if classification in _NEVER_RETRIED_CLASSIFICATIONS:
            if worst is None or count > worst[0]:
                worst = (count, error, str(row.get("oldest_at") or "unknown"))

    stuck = sum(buckets.get(name, 0) for name in _NEVER_RETRIED_CLASSIFICATIONS)
    result["failed_by_classification"] = dict(sorted(buckets.items()))
    result["awaiting_manual_review"] = stuck
    # Broken out of the parked pile because the two need OPPOSITE operator actions: a parse-error
    # pile is a model or prompt problem, a budget pile means the cap is too low for real episodes
    # and is fixed by raising it and requeueing. Folded together they read as one backlog with no
    # indicated action -- which is how the 196-episode pile above stayed unactioned.
    result["parked_on_budget"] = budget_parked

    if stuck > 0 and worst is not None:
        top_count, top_error, oldest_at = worst
        logger.warning(
            "STUCK ENRICHMENT BACKLOG: %d failed episode(s) will NEVER be retried "
            "automatically (%s). Their content is in the graph but has no entities, so it is "
            "invisible to recall even though add_memory reported success. Oldest since %s. "
            "Top cause (%d): %.200s. Fix the root cause, then recover with "
            "scripts/retry_failed_episodes.py --apply.",
            stuck,
            ", ".join(f"{k}={v}" for k, v in sorted(buckets.items()) if k in _NEVER_RETRIED_CLASSIFICATIONS),
            oldest_at,
            top_count,
            top_error.replace("\n", " "),
        )

    return result
