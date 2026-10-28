"""Event-log fold -> timeline View. Deterministic lossless timeline over dated `Event`s.

The counter fold (`fold_events_to_counter`) was removed 2026-10-28 with the consolidation counter
lane (#168); see branch `archive/consolidation-counter-2026-10-28`.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Protocol

from menhir.domain.fold_algebra import Event, timeline
from menhir.infrastructure.view_repository import ViewRepository
from menhir.services.seam_types import Embed

logger = logging.getLogger(__name__)


class _TimelineAdapter(Protocol):
    def record_timeline(self, **kwargs: Any) -> dict[str, Any]: ...


def fold_events_to_timeline(
    *,
    graph_adapter: _TimelineAdapter,
    subject: str,
    events: list[Event],
    namespace: str = "agent-experience",
    source: str = "event-fold",
    embed: Embed | None = None,
) -> dict[str, Any]:
    """Fold dated `events` (e.g. acquisitions) into a lossless TIMELINE View for `subject` — the σ
    WINDOW write half (Lever A). A timeline is stored WITHOUT any window baked in ("last month" is a
    relative frame that changes meaning over time — see the fold-algebra WINDOW note); windowed counts
    are answered at READ time by `windowed_fold.count_in_window` over this payload. The item name
    (`identity`) is carried into each entry's `what` so a read-time DISTINCT-in-window sees the items.
    Idempotent + supersedable: `timeline` dedups by (when, item), re-folding the same events no-ops."""
    # coerce display to the item identity so windowed distinct counts key on the thing, not a quote.
    coerced = [Event(when=e.when, kind=e.kind, value=e.value, identity=e.identity,
                     what=(e.identity or e.what), episode_uuid=e.episode_uuid) for e in events]
    entries = timeline(coerced)  # [{when, what, episode_uuid}] sorted ascending, deduped

    name_embedding = None
    if embed is not None and entries:
        try:
            name_embedding = embed(ViewRepository._timeline_surface(subject, entries))
        except Exception as exc:
            logger.warning(
                "Event-fold timeline embedding failed subject=%s namespace=%s; "
                "writing without embedding (keyword-only retrieval): %s",
                subject, namespace, exc,
            )
            name_embedding = None  # surfacing degrades to BM25-only; never block the write

    res = graph_adapter.record_timeline(
        subject=subject, entries=entries, namespace=namespace, source=source,
        name_embedding=name_embedding,
    )
    res.update({"entries": len(entries)})
    return res
