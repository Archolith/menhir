"""CF-268: query mode anchors on the best hit that can anchor, and never fakes "no match"."""

from __future__ import annotations

from typing import Any

import pytest

from menhir.services.recall_service import RecallService
from menhir.services.scoring_service import ScoringService
from menhir.services.timeline_service import _QUERY_NO_ANCHOR_NOTE, _QUERY_NO_HIT_NOTE

pytestmark = pytest.mark.unit


def _row(uuid: str, valid_at: str) -> dict[str, Any]:
    return {"uuid": uuid, "valid_at": valid_at, "created_at": None, "content": "c",
            "session_id": "s1", "source": "claude-code"}


class _Adapter:
    def __init__(self, rows, hits, involving: set[str] | None = None,
                 unanchorable: set[str] | None = None) -> None:
        self.rows = rows
        self.hits = hits
        self.involving = involving or set()
        self.unanchorable = unanchorable or set()
        self.search_calls: list[dict[str, Any]] = []
        self.anchor_calls: list[tuple[str, str | None]] = []
        self.views: list[dict[str, Any]] = []

    def resolve_timeline_subject(self, *, subject, namespace):
        return [{"uuid": "entity-1", "name": "Rachel"}]

    def search_episode_embeddings(self, query_vector, *, limit=10, namespace=None, model=None,
                                 subject_uuid=None, resolved_only=False):
        self.search_calls.append({"limit": limit, "namespace": namespace, "model": model,
                                  "subject_uuid": subject_uuid,
                                  "resolved_only": resolved_only})
        hits = self.hits
        if subject_uuid:
            hits = [h for h in hits if h["uuid"] in self.involving]
        return hits[:limit]

    def timeline_anchor(self, *, uuid, namespace, subject_uuid=None):
        self.anchor_calls.append((uuid, subject_uuid))
        if uuid in self.unanchorable:
            return None
        if subject_uuid and uuid not in self.involving:
            return None
        return next((dict(r) for r in self.rows if r["uuid"] == uuid), None)

    def timeline_page(self, *, namespace=None, subject_uuid=None, after=None, before=None,
                      window_from=None, window_to=None, limit):
        rows = sorted(self.rows, key=lambda r: r["valid_at"])
        if subject_uuid:
            rows = [r for r in rows if r["uuid"] in self.involving]
        if after is not None:
            rows = [r for r in rows if r["valid_at"] > after[0]]
        if before is not None:
            rows = [r for r in rows if r["valid_at"] < before[0]]
        return rows[-limit:] if before is not None else rows[:limit]

    def timeline_facts(self, *, episode_uuids, namespace):
        return {u: [] for u in episode_uuids}

    def list_recall_visible_scalar_history_views(self, *, subject_uuid, namespace=None):
        return self.views

    def list_scalar_history_views(self, *, subject_uuid, namespace=None):
        return self.views

    def list_scalar_history_entries(self, *, view_uuid, offset=0, limit=16, namespace=None):
        return {"entries": [], "total": 0, "next_offset": None}


class _Graphiti:
    async def embed_query(self, query: str) -> list[float]:
        return [0.1]


def _svc(adapter: _Adapter) -> RecallService:
    return RecallService(
        graphiti_client=_Graphiti(),
        graph_adapter=adapter,  # type: ignore[arg-type]
        scoring_service=ScoringService(),
    )


_ROWS = [_row(f"ep-{i}", f"2026-01-{i:02d}") for i in range(1, 6)]


@pytest.mark.asyncio
async def test_subject_constraint_is_pushed_into_the_search_and_anchors() -> None:
    adapter = _Adapter(_ROWS, [_row("ep-5", "2026-01-05"), _row("ep-3", "2026-01-03")],
                       involving={"ep-1", "ep-2", "ep-3"})
    result = await _svc(adapter).recall_timeline(
        namespace="ns", query="q", subject="rachel", limit=3
    )
    assert [e.uuid for e in result.entries if e.is_anchor] == ["ep-3"]
    call = adapter.search_calls[0]
    assert call["subject_uuid"] == "entity-1"
    assert call["namespace"] == "ns"
    assert call["limit"] > 1


@pytest.mark.asyncio
async def test_subject_query_skips_candidates_that_cannot_anchor() -> None:
    adapter = _Adapter(_ROWS, [_row("ep-4", "2026-01-04"), _row("ep-2", "2026-01-02")],
                       involving={"ep-2", "ep-4"}, unanchorable={"ep-4"})
    result = await _svc(adapter).recall_timeline(
        namespace="ns", query="q", subject="rachel", limit=3
    )
    assert [e.uuid for e in result.entries if e.is_anchor] == ["ep-2"]


@pytest.mark.asyncio
async def test_subject_query_with_nothing_anchorable_returns_exhausted_note() -> None:
    adapter = _Adapter(_ROWS, [_row("ep-4", "2026-01-04")], involving={"ep-4"},
                       unanchorable={"ep-4"})
    result = await _svc(adapter).recall_timeline(
        namespace="ns", query="q", subject="rachel", limit=3
    )
    assert result.entries == ()
    assert result.note == _QUERY_NO_ANCHOR_NOTE
    assert result.note != _QUERY_NO_HIT_NOTE
    assert result.thread == "subject"
    assert result.subject_uuid == "entity-1"


@pytest.mark.asyncio
async def test_empty_outcomes_keep_typed_histories_and_combine_notes() -> None:
    views = [{"uuid": f"view-{i}", "attribute": f"attr-{i}"} for i in range(12)]
    # True no-hits branch.
    none = _Adapter(_ROWS, [], involving=set())
    none.views = views
    r1 = await _svc(none).recall_timeline(namespace="ns", query="q", subject="rachel")
    assert r1.entries == () and len(r1.histories) == 10
    assert r1.note is not None and r1.note.startswith(_QUERY_NO_HIT_NOTE)
    assert "2 more" in r1.note
    # Exhausted-candidates branch.
    exhausted = _Adapter(_ROWS, [_row("ep-4", "2026-01-04")], involving={"ep-4"},
                         unanchorable={"ep-4"})
    exhausted.views = views
    r2 = await _svc(exhausted).recall_timeline(namespace="ns", query="q", subject="rachel")
    assert r2.entries == () and len(r2.histories) == 10
    assert r2.note is not None and r2.note.startswith(_QUERY_NO_ANCHOR_NOTE)
    assert "2 more" in r2.note


@pytest.mark.asyncio
async def test_query_without_subject_skips_store_only_top_hit() -> None:
    adapter = _Adapter(
        _ROWS,
        [_row("store-only", "2026-01-09"), _row("ep-3", "2026-01-03"), _row("ep-4", "2026-01-04")],
        unanchorable={"store-only"},
    )
    result = await _svc(adapter).recall_timeline(namespace="ns", query="q", limit=3)
    assert adapter.search_calls[0]["limit"] > 1
    assert adapter.search_calls[0]["resolved_only"] is True
    assert adapter.search_calls[0]["subject_uuid"] is None
    assert [e.uuid for e in result.entries] == ["ep-2", "ep-3", "ep-4"]
    assert [e.is_anchor for e in result.entries] == [False, True, False]
    assert adapter.anchor_calls == [("store-only", None), ("ep-3", None)]


@pytest.mark.asyncio
async def test_query_without_subject_and_nothing_anchorable_does_not_raise() -> None:
    adapter = _Adapter(_ROWS, [_row("store-only", "2026-01-09")], unanchorable={"store-only"})
    result = await _svc(adapter).recall_timeline(namespace="ns", query="q", limit=3)
    assert result.entries == ()
    assert result.thread == "namespace"
    assert result.note == _QUERY_NO_ANCHOR_NOTE
