"""MCP formatting helpers — pure data transforms and episode polling helpers.

Implementation lives in sibling ``formatters_*.py`` modules; this module re-exports
every helper so existing ``menhir.mcp.formatters`` import sites keep working unchanged.
The stuck-count memoization family stays here because tests reset the module global
``formatters._stuck_count_cache`` directly.
"""

from __future__ import annotations

import logging
from time import monotonic

from menhir.core.reader_identity import normalize_reader_id as _normalize_reader_id
from menhir.domain.models import ProcessingState
from menhir.mcp.formatters_episodes import (
    _collect_episode_status,  # noqa: F401 - compatibility re-export
    _episode_status_guidance,  # noqa: F401 - compatibility re-export
    _format_episode_status,  # noqa: F401 - compatibility re-export
    _format_episode_watch,  # noqa: F401 - compatibility re-export
    _format_row_memory,  # noqa: F401 - compatibility re-export
)
from menhir.mcp.formatters_items import (
    _bd,  # noqa: F401 - compatibility re-export
    _compact_json,  # noqa: F401 - compatibility re-export
    _compact_memory_item,  # noqa: F401 - compatibility re-export
    _compact_scored_item,  # noqa: F401 - compatibility re-export
    _format_when,  # noqa: F401 - compatibility re-export
    _resolve_stale_advisory,  # noqa: F401 - compatibility re-export
    _tf,  # noqa: F401 - compatibility re-export
)
from menhir.mcp.formatters_primitives import (
    _coerce_conflict_members,  # noqa: F401 - compatibility re-export
    _coerce_iso,  # noqa: F401 - compatibility re-export
    _count_unresolved_members,  # noqa: F401 - compatibility re-export
    _node_sort_key,  # noqa: F401 - compatibility re-export
    _parse_graph_datetime,  # noqa: F401 - compatibility re-export
    _require_episode_uuid,  # noqa: F401 - compatibility re-export
    _resolve_conflict_status_filter,  # noqa: F401 - compatibility re-export
    _resolve_queue_state_filter,  # noqa: F401 - compatibility re-export
    _stale_reason_for_row,  # noqa: F401 - compatibility re-export
)

logger = logging.getLogger(__name__)


#: How long a stuck-backlog count is reused before re-querying. `add_memory` is a hot path
#: and the backlog moves slowly, so a stale-by-a-minute number is worth far more than the
#: latency of counting it on every single write.
_STUCK_COUNT_TTL_S = 60.0

#: (monotonic_deadline, count). Module-level so every writer shares one refresh.
_stuck_count_cache: tuple[float, int] | None = None


async def _standing_unrecallable_count(backend: object) -> int:
    """Return how many episodes are currently FAILED, hence holding content with no entities.

    Recall searches `:Entity`, so a FAILED episode's text is in the graph but unreachable --
    and `add_memory` has already told its caller the write succeeded. Surfacing the count in
    the write's own response is the only signal that reaches EVERY client (Claude, Codex,
    opencode, Qwen); a host hook would only cover whichever harness installs it.

    Counts all FAILED, not just the never-retried ones: from the caller's side "waiting for a
    retry" and "parked forever" are the same state -- not recallable right now. The
    terminal/manual_review split is an operator concern and lives in the queue-health warning.

    Best-effort by construction: a failure to count must never fail the write.
    """
    global _stuck_count_cache

    now = monotonic()
    if _stuck_count_cache is not None and now < _stuck_count_cache[0]:
        return _stuck_count_cache[1]

    try:
        if hasattr(backend, "get_queue_depth"):
            overview = await backend.fetch_memory_overview()
        else:
            overview = backend.graph_adapter.fetch_memory_overview()
        count = int((overview or {}).get("failed_count") or 0)
    except Exception:  # noqa: BLE001 - advisory only; never break an ingest response
        logger.debug("could not read standing unrecallable count", exc_info=True)
        return _stuck_count_cache[1] if _stuck_count_cache is not None else 0

    _stuck_count_cache = (now + _STUCK_COUNT_TTL_S, count)
    return count


async def _queue_summary(backend: object) -> str:
    # NOTE: this helper runs AFTER a durable mutation, so it must never raise --
    # its failure is cosmetic and the write is already committed. A suppressed
    # read must surface as unknown, never as a fabricated measurement.
    try:
        if hasattr(backend, "get_queue_depth"):
            queue_depth = int(await backend.get_queue_depth())
            active_rows = await backend.list_episode_processing(states=[ProcessingState.ENRICHING], limit=200)
            snapshot = await backend.scheduler_status_snapshot()
        else:
            queue_depth = int(backend.ingest_service.get_queue_depth())
            active_rows = backend.graph_adapter.list_episode_processing(
                processing_states=[ProcessingState.ENRICHING],
                limit=200,
            )
            scheduler = getattr(backend, "scheduler", None)
            snapshot = None
            if scheduler is not None and hasattr(scheduler, "status_snapshot"):
                try:
                    snapshot = scheduler.status_snapshot()
                except (AttributeError, TypeError, RuntimeError):
                    snapshot = None
    except Exception:  # noqa: BLE001 - post-write cosmetic read; the write is committed
        logger.debug("could not read queue status after durable mutation", exc_info=True)
        return (
            "queue_status: UNAVAILABLE (queue status could not be read after the "
            "write; the write itself is committed)"
        )
    active_enriching = len(active_rows)
    scheduler_state = "running" if snapshot and snapshot.get("running") else "stopped" if snapshot is not None else "unknown"
    summary = (
        f"queue_depth={queue_depth}, "
        f"active_enriching={active_enriching}, "
        f"scheduler={scheduler_state}"
    )

    from menhir.infrastructure.observability import last_provider_auth_failure

    auth_failure = last_provider_auth_failure()
    if auth_failure is not None:
        summary += (
            f"\nWARNING: {auth_failure.summary()}. New memories are stored but cannot be "
            "enriched or recalled until the provider credential (e.g. OPENAI_API_KEY) is fixed; "
            "failed episodes are retried automatically once a call succeeds."
        )
    unrecallable = await _standing_unrecallable_count(backend)
    if unrecallable > 0:
        summary += (
            f"\nWARNING: {unrecallable} earlier "
            f"{'memory is' if unrecallable == 1 else 'memories are'} FAILED and NOT recallable "
            f"-- the text is stored but has no entities, so recall cannot return it. "
            f"This write may end the same way; check get_enrichment_status(episode_id) if it matters."
        )
    return summary
