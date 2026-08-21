"""Event-log fold -> counter View (move 2). Deterministic SUM / COUNT / DISTINCT-COUNT over dated
`Event`s, materialized as a supersedable counter — the exact answer to "how many / how much" where
the user never *stated* a total (total spent on a hobby, items owned, times an activity happened).
These are the cases that need the deterministic fold, not a better model; this is the fold.

Perception (episodes -> typed `Event`s) is the LLM's only role and lives upstream; this service is
pure arithmetic + one `record_counter`. Batch re-fold semantics: it stores the ABSOLUTE reduced
value with `valid_at` = the latest event's world time, so re-running is idempotent at the value
level (Law 2) and the counter's `valid_at` feeds the View's LWW ordering guard (Law 1) correctly.
"""

from __future__ import annotations

from typing import Any, Callable, Protocol

from menhir.domain.fold_algebra import REDUCERS, Event, latest, timeline
from menhir.infrastructure.view_repository import ViewRepository
from menhir.services.seam_types import Embed


#: reducers whose accumulator is the scalar a counter stores.
_SCALAR_REDUCERS = ("sum", "count", "distinct_count")


class _GraphAdapter(Protocol):
    def record_counter(self, **kwargs: Any) -> dict[str, Any]: ...


class _TimelineAdapter(Protocol):
    def record_timeline(self, **kwargs: Any) -> dict[str, Any]: ...


def fold_events_to_counter(
    *,
    graph_adapter: _GraphAdapter,
    subject: str,
    measure: str,
    events: list[Event],
    reducer: str = "sum",
    namespace: str = "agent-experience",
    source: str = "event-fold",
    embed: Embed | None = None,
    audit: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Fold `events` with a scalar reducer and upsert the result as a counter View for
    (subject, measure). `reducer` in {sum, count, distinct_count}. Idempotent + supersedable:
    re-folding the same events no-ops; adding a later-dated event supersedes (its `valid_at` > current,
    so the LWW guard admits it). Returns the counter row + {reducer, value, events}."""
    if reducer not in _SCALAR_REDUCERS:
        raise ValueError(f"reducer {reducer!r} must be one of {_SCALAR_REDUCERS}")
    fn, _idempotent = REDUCERS[reducer]
    value = float(fn(events))  # batch re-fold: absolute value over the full set (Law-2 safe)
    lat = latest(events)
    valid_at = lat.when if lat is not None else None
    episode_uuids = [e.episode_uuid for e in events if e.episode_uuid]

    name_embedding = None
    if embed is not None:
        try:
            name_embedding = embed(ViewRepository.retrieval_text(subject, measure, value))
        except Exception:
            name_embedding = None  # surfacing degrades to BM25-only; never block the write

    res = graph_adapter.record_counter(
        subject=subject, counter=measure, value=value, namespace=namespace,
        valid_at=valid_at, episode_uuids=episode_uuids, source=source,
        name_embedding=name_embedding, audit=audit,
    )
    res.update({"reducer": reducer, "value": value, "events": len(events)})
    return res


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
        except Exception:
            name_embedding = None  # surfacing degrades to BM25-only; never block the write

    res = graph_adapter.record_timeline(
        subject=subject, entries=entries, namespace=namespace, source=source,
        name_embedding=name_embedding,
    )
    res.update({"entries": len(entries)})
    return res
