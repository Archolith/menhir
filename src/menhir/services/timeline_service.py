"""`recall_timeline` service: on-demand time navigation over recorded memory time.

Phase A of .agent/plans/menhir-recall-timeline-plan.md. Read-only like
`run_recall_history`: failures RAISE, no partial results, no `last_accessed`
touches, no access updates. Timeline order is RECORDED time (episode `valid_at`)
per the owner ruling in the plan; `recall_history` is unchanged.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from menhir.domain.event_time import rendered_event_time
from menhir.domain.namespace import namespace_to_group_ids
from menhir.domain.timeline import (
    TimelineEntry,
    TimelineFact,
    TimelineResult,
    TypedHistory,
    TypedHistoryEntry,
    decode_cursor,
    encode_cursor,
)
from menhir.services.recall_pipeline import (
    _RECALL_HISTORY_MAX_CHARS,
    _embedding_model_kwargs,
)

#: Headline cap: whitespace-collapsed content, then a trailing ellipsis.
_HEADLINE_MAX_CHARS = 160

#: +inf sentinels for the inclusive-equal `at`/latest-page boundaries (plan A5).
_MAX_INSTANT = "9999-12-31T23:59:59.999999+00:00"
_MAX_UUID = "￿"
_EPOCH = "1970-01-01T00:00:00Z"

#: At most this many typed histories are paged beside a subject timeline.
_TYPED_HISTORY_CAP = 10

_QUERY_NO_HIT_NOTE = "No embedded memories matched, so there is no timeline to anchor on."

_HISTORY_TRUNCATION_NOTE = "Only the first {shown} typed histories are shown; {hidden} more exist."


def _validate_instant(name: str, value: Any) -> str:
    text = str(value).strip()
    # Accept the tool's own recorded_at format: Neo4j renders zoned datetimes with a trailing
    # `[Zone]` (e.g. `2026-01-01T01:20:00Z[UTC]`), which Cypher's datetime() parses but Python
    # does not; validate without the suffix and pass the original through.
    bare = text.split("[", 1)[0]
    try:
        datetime.fromisoformat(bare.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be a valid ISO instant, got {value!r}") from exc
    return text


def _row_key(row: dict[str, Any]) -> tuple[str, str, str]:
    """The A1 total-order key of a timeline row: (valid_at, created_at|epoch, uuid)."""
    created_at = row.get("created_at")
    return (str(row["valid_at"]), str(created_at) if created_at is not None else _EPOCH,
            str(row["uuid"]))


def _collapse(text: Any) -> str:
    return " ".join(str(text or "").split())


def _entry_from_row(
    row: dict[str, Any],
    *,
    detail: str,
    facts_map: dict[str, list[dict[str, Any]]],
    is_anchor: bool = False,
    event_time: bool = False,
) -> TimelineEntry:
    raw = _collapse(row.get("content"))
    headline = raw[:_HEADLINE_MAX_CHARS] + "…" if len(raw) > _HEADLINE_MAX_CHARS else raw
    if detail == "full":
        content = (
            raw[:_RECALL_HISTORY_MAX_CHARS] + "…"
            if len(raw) > _RECALL_HISTORY_MAX_CHARS else raw
        )
    else:
        content = None
    facts = tuple(
        TimelineFact(
            fact=str(f.get("fact") or ""),
            valid_at=f.get("valid_at"),
            invalid_at=f.get("invalid_at"),
            expired_at=f.get("expired_at"),
            event_time=rendered_event_time(f) if event_time else None,
        )
        for f in (facts_map.get(str(row["uuid"])) or [])
    )
    return TimelineEntry(
        uuid=str(row["uuid"]),
        recorded_at=str(row["valid_at"]),
        created_at=row.get("created_at"),
        session_id=row.get("session_id"),
        source=row.get("source"),
        headline=headline,
        content=content,
        facts=facts,
        is_anchor=is_anchor,
    )


async def _probe_page(
    adapter: Any,
    *,
    namespace: str | None,
    subject_uuid: str | None,
    side: str,
    key: tuple[str, str | None, str] | None,
    count: int,
    window_from: str | None = None,
    window_to: str | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """One directional page of `limit + 1` rows so the caller knows if more exist.

    `side` is "after" (ascending from an exclusive key) or "before" (descending to an
    exclusive key; the repository reverses so rows always come back ascending).
    Returns `(rows, has_more)` with rows trimmed back to `count`.
    """
    kwargs: dict[str, Any] = {
        "namespace": namespace,
        "subject_uuid": subject_uuid,
        "window_from": window_from,
        "window_to": window_to,
        "limit": count + 1,
    }
    if key is not None:
        kwargs["after" if side == "after" else "before"] = key
    rows = await asyncio.to_thread(adapter.timeline_page, **kwargs)
    has_more = len(rows) > count
    # Rows always arrive ASCENDING (the repository reverses its DESC `before` query
    # internally). The rows closest to the boundary key are the last ones for a
    # `before` page and the first ones for an `after` page.
    trimmed = rows[-count:] if side == "before" else rows[:count]
    return list(trimmed), has_more


async def _typed_histories(
    adapter: Any,
    *,
    subject_uuid: str,
    gid: str | None,
    history_view: str | None,
    history_offset: int | None,
    history_limit: int,
) -> tuple[tuple[TypedHistory, ...], str | None]:
    """Page the subject's typed scalar/event histories beside the timeline (plan A5).

    The default page is the LATEST `history_limit` entries of each View. At most
    `_TYPED_HISTORY_CAP` histories are returned; the caller gets a note when more exist.
    """
    scalar_views = await asyncio.to_thread(
        adapter.list_scalar_history_views, subject_uuid=subject_uuid, namespace=gid
    )
    event_views = await asyncio.to_thread(
        adapter.list_event_timeline_views, subject_uuid=subject_uuid, namespace=gid
    )
    descriptors: list[dict[str, Any]] = []
    for view in scalar_views:
        descriptors.append({
            "kind": "scalar",
            "view_uuid": str(view["uuid"]),
            "label": str(view.get("attribute") or view.get("view_key") or view["uuid"]),
        })
    for view in event_views:
        predicate = str(view.get("predicate") or "")
        domain = view.get("domain")
        descriptors.append({
            "kind": "event",
            "view_uuid": str(view["uuid"]),
            "label": f"{predicate}[{domain}]" if domain else predicate,
        })
    if history_view is not None:
        descriptors = [d for d in descriptors if d["view_uuid"] == history_view]
    hidden = len(descriptors) - _TYPED_HISTORY_CAP
    descriptors = descriptors[:_TYPED_HISTORY_CAP]

    histories: list[TypedHistory] = []
    for descriptor in descriptors:
        kind = descriptor["kind"]
        view_uuid = descriptor["view_uuid"]
        if kind == "scalar":
            reader = adapter.list_scalar_history_entries
        else:
            reader = adapter.list_event_timeline_entries
        first = await asyncio.to_thread(
            reader, view_uuid=view_uuid, offset=0, limit=1, namespace=gid
        )
        total = int(first.get("total") or 0)
        offset = history_offset if history_offset is not None else max(0, total - history_limit)
        offset = max(0, int(offset))
        page = await asyncio.to_thread(
            reader, view_uuid=view_uuid, offset=offset, limit=history_limit, namespace=gid
        )
        entries = tuple(
            TypedHistoryEntry(
                valid_at=str(raw.get("valid_at") or ""),
                value=raw.get("object_display") if kind == "event" else raw.get("value"),
                operation=raw.get("operation") if kind == "scalar" else None,
                time_basis=raw.get("time_basis") if kind == "event" else None,
                quote=raw.get("quote") if kind == "event" else raw.get("stated_span"),
                episode_uuid=raw.get("episode_uuid"),
            )
            for raw in (page.get("entries") or [])
        )
        prev_offset = offset - history_limit if offset > 0 else None
        histories.append(TypedHistory(
            kind=kind,
            view_uuid=view_uuid,
            label=descriptor["label"],
            total=total,
            offset=offset,
            entries=entries,
            prev_offset=prev_offset,
            next_offset=page.get("next_offset"),
        ))
    note = _HISTORY_TRUNCATION_NOTE.format(shown=len(histories), hidden=hidden) if hidden > 0 else None
    return tuple(histories), note


async def run_recall_timeline(
    service: Any,
    *,
    namespace: str | None = None,
    query: str | None = None,
    subject: str | None = None,
    at: str | None = None,
    window_from: str | None = None,
    window_to: str | None = None,
    around: str | None = None,
    cursor: str | None = None,
    direction: str = "both",
    limit: int = 10,
    detail: str = "headline",
    facts: bool = False,
    history_view: str | None = None,
    history_offset: int | None = None,
    history_limit: int = 10,
    event_time: bool = False,
) -> TimelineResult:
    """Navigate a recorded-time thread of visible memories on demand.

    Exactly one starting point of `at` / (`window_from` and/or `window_to`) / `around`
    / `cursor` / `query` is required when any is given; `subject` is a filter that
    combines with every mode and restricts it to the subject thread (`subject` alone
    means the latest page of the subject thread). Read-only: no access updates, no
    partial results -- graph or embedding failures raise.
    """
    adapter = service.graph_adapter
    if detail not in ("headline", "full"):
        raise ValueError("detail must be 'headline' or 'full'")
    if direction not in ("before", "after", "both"):
        raise ValueError("direction must be one of 'before', 'after', 'both'")
    safe_limit = max(1, min(int(limit), 50))
    safe_history_limit = max(1, min(int(history_limit), 50))

    if at is not None:
        at = _validate_instant("at", at)
    if window_from is not None:
        window_from = _validate_instant("window_from", window_from)
    if window_to is not None:
        window_to = _validate_instant("window_to", window_to)

    starts = [
        name for name, given in (
            ("at", at is not None),
            ("window", window_from is not None or window_to is not None),
            ("around", around is not None),
            ("cursor", cursor is not None),
            ("query", query is not None and str(query).strip() != ""),
        ) if given
    ]
    if len(starts) > 1:
        raise ValueError(
            "exactly one of at / window_from+window_to / around / cursor / query is required"
        )
    if not starts and subject is None:
        raise ValueError(
            "exactly one of at / window_from+window_to / around / cursor / query / subject "
            "is required"
        )

    namespace_key = "|".join(namespace_to_group_ids(namespace) or ["*"])
    subject_uuid: str | None = None
    subject_name: str | None = None
    if subject is not None:
        rows = await asyncio.to_thread(
            adapter.resolve_timeline_subject, subject=subject, namespace=namespace
        )
        if not rows:
            raise ValueError("unknown subject")
        if len(rows) > 1:
            candidates = ", ".join(
                f"{r.get('name') or r.get('uuid')} ({r.get('uuid')})" for r in rows
            )
            raise ValueError(f"ambiguous subject; candidates: {candidates}")
        subject_uuid = str(rows[0]["uuid"])
        subject_name = rows[0].get("name")

    note: str | None = None
    before_rows: list[dict[str, Any]] = []
    after_rows: list[dict[str, Any]] = []
    before_more = after_more = False
    before_probed = after_probed = False
    anchor_row: dict[str, Any] | None = None

    mode = starts[0] if starts else "subject"
    if mode == "at":
        boundary = (at, _MAX_INSTANT, _MAX_UUID)
        count_before = safe_limit // 2 if direction == "both" else (
            safe_limit if direction == "before" else 0)
        count_after = safe_limit - count_before
        if count_before:
            before_probed = True
            before_rows, before_more = await _probe_page(
                adapter, namespace=namespace, subject_uuid=subject_uuid,
                side="before", key=boundary, count=count_before,
            )
        if count_after:
            after_probed = True
            after_rows, after_more = await _probe_page(
                adapter, namespace=namespace, subject_uuid=subject_uuid,
                side="after", key=boundary, count=count_after,
            )
    elif mode == "window":
        after_probed = True
        after_rows, after_more = await _probe_page(
            adapter, namespace=namespace, subject_uuid=subject_uuid,
            side="after", key=None, count=safe_limit,
            window_from=window_from, window_to=window_to,
        )
    elif mode == "around":
        anchor_row = await asyncio.to_thread(
            adapter.timeline_anchor, uuid=around, namespace=namespace,
            subject_uuid=subject_uuid,
        )
        if anchor_row is None:
            raise ValueError("unknown or hidden memory")
        anchor_key = _row_key(anchor_row)
        count_before = safe_limit // 2
        # The anchor occupies one slot of the page, so the split mirrors `at` minus one.
        count_after = safe_limit - count_before - 1
        if count_before:
            before_probed = True
            before_rows, before_more = await _probe_page(
                adapter, namespace=namespace, subject_uuid=subject_uuid,
                side="before", key=anchor_key, count=count_before,
            )
        if count_after:
            after_probed = True
            after_rows, after_more = await _probe_page(
                adapter, namespace=namespace, subject_uuid=subject_uuid,
                side="after", key=anchor_key, count=count_after,
            )
    elif mode == "cursor":
        (c_valid, c_created, c_uuid), cursor_direction = decode_cursor(
            str(cursor), namespace_key=namespace_key, subject_uuid=subject_uuid
        )
        cursor_key = (c_valid, c_created, c_uuid)
        if cursor_direction == "after":
            after_probed = True
            after_rows, after_more = await _probe_page(
                adapter, namespace=namespace, subject_uuid=subject_uuid,
                side="after", key=cursor_key, count=safe_limit,
            )
        else:
            before_probed = True
            before_rows, before_more = await _probe_page(
                adapter, namespace=namespace, subject_uuid=subject_uuid,
                side="before", key=cursor_key, count=safe_limit,
            )
    elif mode == "query":
        stripped = str(query).strip()
        query_vector = await service.graphiti_client.embed_query(stripped)
        # The hit is a receipt (queue-node) uuid; `timeline_anchor` resolves it (T1).
        hits = await asyncio.to_thread(
            adapter.search_episode_embeddings, query_vector, limit=1, namespace=namespace,
            **_embedding_model_kwargs(service),
        )
        if not hits:
            return TimelineResult(
                thread="subject" if subject_uuid else "namespace",
                subject_uuid=subject_uuid,
                subject_name=subject_name,
                entries=(),
                prev_cursor=None,
                next_cursor=None,
                histories=(),
                note=_QUERY_NO_HIT_NOTE,
            )
        seed_uuid = str(hits[0]["uuid"])
        anchor_row = await asyncio.to_thread(
            adapter.timeline_anchor, uuid=seed_uuid, namespace=namespace,
            subject_uuid=subject_uuid,
        )
        if anchor_row is None:
            raise ValueError("unknown or hidden memory")
        anchor_key = _row_key(anchor_row)
        count_before = safe_limit // 2
        count_after = safe_limit - count_before - 1
        if count_before:
            before_probed = True
            before_rows, before_more = await _probe_page(
                adapter, namespace=namespace, subject_uuid=subject_uuid,
                side="before", key=anchor_key, count=count_before,
            )
        if count_after:
            after_probed = True
            after_rows, after_more = await _probe_page(
                adapter, namespace=namespace, subject_uuid=subject_uuid,
                side="after", key=anchor_key, count=count_after,
            )
    else:  # subject alone: the latest page of the subject thread
        before_probed = True
        before_rows, before_more = await _probe_page(
            adapter, namespace=namespace, subject_uuid=subject_uuid,
            side="before", key=(_MAX_INSTANT, _MAX_INSTANT, _MAX_UUID), count=safe_limit,
        )

    page_rows = before_rows + ([anchor_row] if anchor_row is not None else []) + after_rows
    anchor_uuid = str(anchor_row["uuid"]) if anchor_row is not None else None

    facts_map: dict[str, list[dict[str, Any]]] = {}
    if facts and page_rows:
        # The kwarg is passed only when on, so the flag-off adapter call is unchanged.
        facts_map = await asyncio.to_thread(
            adapter.timeline_facts,
            episode_uuids=[str(r["uuid"]) for r in page_rows],
            namespace=namespace,
            **({"event_time": True} if event_time else {}),
        )
    entries = tuple(
        _entry_from_row(
            row,
            detail=detail,
            facts_map=facts_map,
            is_anchor=(anchor_uuid is not None and str(row["uuid"]) == anchor_uuid),
            event_time=event_time,
        )
        for row in page_rows
    )

    prev_cursor: str | None = None
    next_cursor: str | None = None
    if entries:
        first_key = _row_key(page_rows[0])
        last_key = _row_key(page_rows[-1])
        prev_cursor = (
            encode_cursor(first_key, "before", namespace_key, subject_uuid)
            if not before_probed or before_more else None
        )
        next_cursor = (
            encode_cursor(last_key, "after", namespace_key, subject_uuid)
            if not after_probed or after_more else None
        )

    histories: tuple[TypedHistory, ...] = ()
    if subject_uuid is not None:
        group_ids = namespace_to_group_ids(namespace)
        gid = group_ids[0] if group_ids is not None else None
        histories, history_note = await _typed_histories(
            adapter,
            subject_uuid=subject_uuid,
            gid=gid,
            history_view=history_view,
            history_offset=history_offset,
            history_limit=safe_history_limit,
        )
        if history_note is not None:
            note = history_note

    return TimelineResult(
        thread="subject" if subject_uuid else "namespace",
        subject_uuid=subject_uuid,
        subject_name=subject_name,
        entries=entries,
        prev_cursor=prev_cursor,
        next_cursor=next_cursor,
        histories=histories,
        note=note,
    )
