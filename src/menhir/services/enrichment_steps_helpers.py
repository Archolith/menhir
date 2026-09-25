"""Pure helpers for the enrichment pipeline (no EnrichmentContext needed).

Extracted verbatim from ``enrichment_steps.py``; the facade module re-exports everything
here so existing ``menhir.services.enrichment_steps`` import sites keep working unchanged.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from menhir.domain.models import ProcessingState
from menhir.domain.utils import source_confidence_for
from menhir.infrastructure import MemoryGraphAdapter
from menhir.services.ingest_limits import MAX_DIFF_CHARS

#: Same logger object/name as the facade module: every record these helpers emit must keep
#: the ``menhir.services.enrichment_steps`` logger name (caplog filters key off it).
logger = logging.getLogger("menhir.services.enrichment_steps")


# ---------------------------------------------------------------------------
# Pure helpers (no ctx needed)
# ---------------------------------------------------------------------------

def failure_details_from_exception(exc: Exception) -> dict[str, object]:
    """Extract optional structured diagnostics carried on enrichment exceptions."""

    details = getattr(exc, "menhir_failure_details", None)
    if isinstance(details, dict):
        return dict(details)
    return {}


def record_retention_sources(
    graph_adapter: Any,
    node_uuids: list[str],
    *,
    source_episode_uuid: str,
    namespace: str,
) -> None:
    """Record source provenance without copying the source's mutable flag state."""

    graph_adapter.record_retention_sources(
        source_episode_uuid=source_episode_uuid,
        entity_uuids=node_uuids,
        namespace=namespace,
    )


def compose_episode_body(claimed: dict[str, object]) -> str:
    """Build the episode body sent to Graphiti, appending any attached diff."""

    content = str(claimed.get("content") or "")
    diff = claimed.get("diff")
    if not diff:
        return content
    diff_text = str(diff).strip()
    if not diff_text:
        return content
    if len(diff_text) > MAX_DIFF_CHARS:
        diff_text = diff_text[:MAX_DIFF_CHARS] + "\n... [diff truncated]"
    return f"{content}\n\n--- git diff ---\n{diff_text}"


def coerce_reference_time(value: object | None) -> datetime:
    """Convert stored graph timestamps to a timezone-aware Python datetime.

    `None` is the EXPECTED default: a live turn has no `occurred_at` and "now" is the correct world
    time for it. A NON-None value that is not a datetime is different -- a caller supplied a time and
    it is being discarded, so the episode will be stamped with ingestion time instead of when it
    actually happened. That must be loud.

    Silence here cost a corpus. On 2026-07-02 archolith-bench recorded that menhir "does NOT backdate
    a fresh benchmark ingest" and shipped a backfill script rather than a fix; the scalar-ku corpus
    built on 2026-07-22 landed with 1707 of 2862 episodes (62%) carrying ingestion time as `valid_at`.
    Supersession orders by `valid_at`, so "which value is current" was decided by ingest order for
    most of that corpus -- and nothing anywhere logged above DEBUG while it happened.
    """

    if hasattr(value, "to_native"):
        value = value.to_native()
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    if value is not None:
        logger.warning(
            "reference_time %r (%s) is not a datetime; stamping this episode with ingestion time. "
            "Backdated history will supersede in the WRONG order.",
            value, type(value).__name__,
        )
    return datetime.now(timezone.utc)


def estimate_episode_tokens(episode_body: str) -> int:
    """Estimate prompt tokens conservatively from raw text length."""

    rendered = str(episode_body or "")
    if not rendered:
        return 0
    return max(1, (len(rendered) + 3) // 4)


def build_episode_preflight_rejection(
    episode_body: str,
    max_estimated_tokens: int,
) -> dict[str, int | str] | None:
    """Return a synthetic terminal error when the raw episode is obviously too large.

    Takes explicit params (not ctx) because the legacy ``ingest_episode()``
    path also calls this function.
    """

    limit = max(0, int(max_estimated_tokens))
    if limit == 0:
        return None

    rendered = str(episode_body or "")
    char_count = len(rendered)
    estimated_tokens = estimate_episode_tokens(rendered)
    if estimated_tokens <= limit:
        return None

    return {
        "code": "episode_preflight_too_large",
        "error": (
            "episode_preflight_too_large "
            f"estimated_tokens={estimated_tokens} limit={limit} chars={char_count}"
        ),
        "estimated_tokens": estimated_tokens,
        "limit": limit,
        "char_count": char_count,
    }


def still_owns_episode(
    graph_adapter: MemoryGraphAdapter,
    episode_uuid: str,
    worker_id: str,
) -> bool:
    """Check whether this worker still holds the enrichment lease."""

    row = graph_adapter.fetch_episode_processing(episode_uuid)
    if row is None:
        return False
    return (
        row.get("processing_state") == ProcessingState.ENRICHING
        and str(row.get("processing_owner") or "") == worker_id
    )
