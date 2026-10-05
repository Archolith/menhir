"""Phase A coverage for `recall_timeline`: query predicates, cursor codec, service."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from menhir.domain.timeline import (
    TimelineEntry,
    TimelineResult,
    decode_cursor,
    encode_cursor,
)
from menhir.infrastructure.memory_queries import MemoryQueryRepository
from menhir.services.recall_service import RecallService
from menhir.services.scoring_service import ScoringService

pytestmark = pytest.mark.unit

PREFILTER = "(n.namespace IN $tenant_namespaces OR n.group_id IN $tenant_namespaces)"
TENANT_SCOPE = (
    "$tenant_namespaces IS NULL OR coalesce({v}.namespace, {v}.group_id, '') "
    "IN $tenant_namespaces"
)
MAX_INSTANT = "9999-12-31T23:59:59.999999+00:00"
MAX_UUID = "￿"
EPOCH = "1970-01-01T00:00:00Z"


# ---------------------------------------------------------------------------
# Query-string unit tests (pattern: tests/test_source_memory_latency_171.py)
# ---------------------------------------------------------------------------


@dataclass
class _RecordingRepository:
    rows: list[dict[str, object]] = field(default_factory=list)
    calls: list[tuple[str, dict[str, object] | None]] = field(default_factory=list)

    def execute(self, query: str, params: dict[str, object] | None = None):
        self.calls.append((query, params))
        return self.rows


def _repository() -> tuple[MemoryQueryRepository, _RecordingRepository]:
    neo4j = _RecordingRepository()
    return MemoryQueryRepository(neo4j), neo4j  # type: ignore[arg-type]


def test_timeline_page_scoped_has_prefilter_first_and_membership() -> None:
    repository, neo4j = _repository()
    repository.timeline_page(namespace="tenant-a", limit=5)
    query, _params = neo4j.calls[0]
    assert PREFILTER in query
    assert TENANT_SCOPE.format(v="n") in query
    assert query.index(PREFILTER) < query.index(TENANT_SCOPE.format(v="n"))


def test_timeline_page_unscoped_has_no_prefilter() -> None:
    repository, neo4j = _repository()
    repository.timeline_page(namespace=None, limit=5)
    query, _params = neo4j.calls[0]
    assert PREFILTER not in query
    assert TENANT_SCOPE.format(v="n") in query


def test_timeline_page_no_hand_written_namespace_comparison() -> None:
    repository, neo4j = _repository()
    repository.timeline_page(namespace="tenant-a", limit=5)
    query, _params = neo4j.calls[0]
    # No hand-written namespace/group_id comparison is allowed anywhere in the query;
    # the only namespace mentions come from the shared predicate fragments
    # (prefilter's `IN $tenant_namespaces`, tenant_scope's coalesce form, and whatever
    # the shared visibility/structural predicates themselves carry).
    assert "n.namespace =" not in query
    assert "n.group_id =" not in query
    assert TENANT_SCOPE.format(v="n") in query
    assert PREFILTER in query


def test_timeline_page_mentions_join_only_with_subject() -> None:
    repository, neo4j = _repository()
    repository.timeline_page(namespace=None, limit=5)
    assert "MATCH (n:Episodic)" in neo4j.calls[0][0]
    assert "<-[:MENTIONS]-(n:Episodic)" not in neo4j.calls[0][0]
    repository.timeline_page(namespace=None, subject_uuid="entity-1", limit=5)
    assert "MATCH (s:Entity {uuid:$subject_uuid})<-[:MENTIONS]-(n:Episodic)" in neo4j.calls[1][0]


def test_timeline_page_uses_total_order_and_exclusive_keys() -> None:
    repository, neo4j = _repository()
    repository.timeline_page(
        namespace=None,
        after=("2026-01-02T00:00:00Z", None, "ep-9"),
        limit=5,
    )
    query, params = neo4j.calls[0]
    assert params is not None and params["after_created_at"] is None
    before = _repository()
    before[0].timeline_page(
        namespace=None,
        before=("2026-01-02T00:00:00Z", "2026-01-01T00:00:00Z", "ep-1"),
        limit=5,
    )
    b_query, b_params = before[1].calls[0]
    assert b_query.index("ORDER BY") < b_query.index("DESC")
    assert b_params["before_uuid"] == "ep-1"


def test_timeline_page_no_tuple_comparison_explicit_or_form() -> None:
    repository, neo4j = _repository()
    repository.timeline_page(
        namespace=None,
        after=("2026-01-02T00:00:00Z", None, "ep-9"),
        before=("2026-01-01T00:00:00Z", None, "ep-1"),
        limit=5,
    )
    query, _params = neo4j.calls[0]
    assert ") > (" not in query
    assert ") < (" not in query
    assert "n.uuid > $after_uuid" in query
    assert "n.uuid < $before_uuid" in query
    assert "n.valid_at > datetime($after_valid_at)" in query
    assert "n.valid_at < datetime($before_valid_at)" in query


def test_timeline_anchor_uses_same_predicates() -> None:
    repository, neo4j = _repository()
    repository.timeline_anchor(uuid="ep-1", namespace="tenant-a")
    query, _params = neo4j.calls[0]
    assert PREFILTER in query
    assert TENANT_SCOPE.format(v="n") in query
    assert "n.uuid = target_uuid" in query


def test_timeline_anchor_resolves_receipt_only_through_a_visible_scoped_queue_node() -> None:
    # T1: a receipt (queue-node) uuid resolves to its Graphiti episode only when that queue node
    # is itself a queue node, visible, non-FAILED and in the caller's silo.
    repository, neo4j = _repository()
    repository.timeline_anchor(uuid="receipt-1", namespace="tenant-a")
    query, params = neo4j.calls[0]
    assert params is not None and params["anchor_uuid"] == "receipt-1"
    resolve = query[query.index("OPTIONAL MATCH (a:Episodic)"):query.index("WITH coalesce(")]
    assert "a.uuid = $anchor_uuid" in resolve
    assert "a.valid_at IS NULL" in resolve
    assert "coalesce(a.processing_state, '') <> 'FAILED'" in resolve
    assert "coalesce(a.scope, 'PERSISTENT') <> 'CANDIDATE'" in resolve
    assert TENANT_SCOPE.format(v="a") in resolve
    assert "coalesce(a.resolved_episode_uuid, $anchor_uuid) AS target_uuid" in query


def test_timeline_facts_scopes_on_edge_and_caps_episodes() -> None:
    repository, neo4j = _repository()
    repository.timeline_facts(
        episode_uuids=[f"ep-{i}" for i in range(60)], namespace="tenant-a"
    )
    query, params = neo4j.calls[0]
    assert TENANT_SCOPE.format(v="r") in query
    assert params is not None
    assert len(params["episode_uuids"]) == 50


def test_timeline_facts_empty_input_skips_query() -> None:
    repository, neo4j = _repository()
    assert repository.timeline_facts(episode_uuids=[], namespace=None) == {}
    assert neo4j.calls == []


def test_timeline_facts_anchored_on_page_episodes() -> None:
    repository, neo4j = _repository()
    repository.timeline_facts(episode_uuids=["ep-1"], namespace=None)
    query, _params = neo4j.calls[0]
    assert "(ep:Episodic {uuid: episode_uuid})" in query


def test_resolve_timeline_subject_predicates() -> None:
    repository, neo4j = _repository()
    repository.resolve_timeline_subject(subject="Rachel", namespace=None)
    query, _params = neo4j.calls[0]
    assert TENANT_SCOPE.format(v="n") in query
    assert "toLower(n.name) = toLower($subject)" in query
    assert "NOT coalesce(n.is_view, false)" in query
    assert "n.uuid = $subject" in query
    assert "LIMIT $limit" in query


# ---------------------------------------------------------------------------
# Cursor codec
# ---------------------------------------------------------------------------


def _key(valid_at: str = "2026-01-02T00:00:00Z", uuid: str = "ep-1",
         created_at: str | None = "2026-01-02T01:00:00Z") -> tuple[str, str | None, str]:
    return (valid_at, created_at, uuid)


def test_cursor_round_trip() -> None:
    token = encode_cursor(_key(), "after", "tenant-a", "subject-1")
    key, direction = decode_cursor(token, namespace_key="tenant-a", subject_uuid="subject-1")
    assert key == _key()
    assert direction == "after"


def test_cursor_none_created_at_round_trips() -> None:
    token = encode_cursor((_key()[0], None, _key()[2]), "before", "", "")
    key, direction = decode_cursor(token, namespace_key="", subject_uuid=None)
    assert key[1] is None and direction == "before"


def test_cursor_rejects_garbage_and_tamper() -> None:
    with pytest.raises(ValueError):
        decode_cursor("not-a-cursor", namespace_key="", subject_uuid=None)
    token = encode_cursor(_key(), "after", "", "")
    payload = list(token)
    payload[5] = "A" if payload[5] != "A" else "B"
    with pytest.raises(ValueError):
        decode_cursor("".join(payload), namespace_key="", subject_uuid=None)


def test_cursor_rejects_unknown_direction() -> None:
    with pytest.raises(ValueError):
        encode_cursor(_key(), "sideways", "", "")


def test_cursor_namespace_mismatch_rejected() -> None:
    token = encode_cursor(_key(), "after", "tenant-a", "")
    with pytest.raises(ValueError):
        decode_cursor(token, namespace_key="tenant-b", subject_uuid=None)


def test_cursor_subject_mismatch_rejected() -> None:
    token = encode_cursor(_key(), "after", "", "subject-1")
    with pytest.raises(ValueError):
        decode_cursor(token, namespace_key="", subject_uuid="subject-2")


def test_default_and_empty_namespace_share_ns_key() -> None:
    from menhir.domain.namespace import namespace_to_group_ids

    assert "|".join(namespace_to_group_ids("default") or ["*"]) == "|".join(
        namespace_to_group_ids("") or ["*"]
    )
    token_default = encode_cursor(_key(), "after", "|".join(
        namespace_to_group_ids("default") or ["*"]), "")
    key, _ = decode_cursor(token_default, namespace_key="|".join(
        namespace_to_group_ids("") or ["*"]), subject_uuid=None)
    assert key == _key()


def test_cursor_unscoped_ns_key_is_star() -> None:
    from menhir.domain.namespace import namespace_to_group_ids

    assert "|".join(namespace_to_group_ids(None) or ["*"]) == "*"


# ---------------------------------------------------------------------------
# Service with a stub adapter
# ---------------------------------------------------------------------------


def _row(
    uuid: str,
    valid_at: str,
    *,
    created_at: str | None = None,
    content: str = "content",
    session_id: str | None = "s1",
    source: str | None = "claude-code",
) -> dict[str, Any]:
    return {
        "uuid": uuid,
        "valid_at": valid_at,
        "created_at": created_at,
        "content": content,
        "session_id": session_id,
        "source": source,
    }


def _sort_key(row: dict[str, Any]) -> tuple[str, str, str]:
    return (row["valid_at"], row["created_at"] or EPOCH, row["uuid"])


class _StubAdapter:
    """Fake MemoryGraphAdapter emulating the timeline repository semantics."""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []
        self.visible_subjects: set[str] = {r["uuid"] for r in self.rows}
        self.anchor_hidden: set[str] = set()
        self.facts: dict[str, list[dict[str, Any]]] = {}
        self.search_hits: list[dict[str, Any]] = []
        self.subject_candidates: list[dict[str, Any]] = []
        self.scalar_views: list[dict[str, Any]] = []
        self.event_views: list[dict[str, Any]] = []
        self.scalar_entries: dict[str, list[dict[str, Any]]] = {}
        self.page_calls: list[dict[str, Any]] = []
        self.facts_calls: list[dict[str, Any]] = []
        self.history_calls: list[dict[str, Any]] = []
        self.view_calls: list[dict[str, Any]] = []
        self.touched: list[str] = []

    def timeline_page(self, *, namespace=None, subject_uuid=None, after=None, before=None,
                      window_from=None, window_to=None, limit):
        self.page_calls.append({"subject_uuid": subject_uuid, "after": after,
                                "before": before, "window_from": window_from,
                                "window_to": window_to, "limit": limit})
        rows = [r for r in self.rows if r["valid_at"] is not None]
        if subject_uuid:
            rows = [r for r in rows if r["uuid"] in self.visible_subjects]
        if after is not None:
            rows = [r for r in rows if _sort_key(r) > (after[0], after[1] or EPOCH, after[2])]
        if before is not None:
            rows = [r for r in rows if _sort_key(r) < (before[0], before[1] or EPOCH, before[2])]
        if window_from is not None:
            rows = [r for r in rows if r["valid_at"] >= window_from]
        if window_to is not None:
            rows = [r for r in rows if r["valid_at"] <= window_to]
        rows = sorted(rows, key=_sort_key)
        if before is not None:
            # The real repository returns `before` pages ASCENDING too (it reverses
            # its DESC query internally), so the service must not re-reverse.
            return rows[-limit:]
        return rows[:limit]

    def timeline_anchor(self, *, uuid, namespace, subject_uuid=None):
        if uuid in self.anchor_hidden:
            return None
        matches = [r for r in self.rows if r["uuid"] == uuid]
        if not matches:
            return None
        return dict(matches[0])

    def timeline_facts(self, *, episode_uuids, namespace):
        self.facts_calls.append({"episode_uuids": list(episode_uuids), "namespace": namespace})
        return {u: self.facts.get(u, []) for u in episode_uuids}

    def resolve_timeline_subject(self, *, subject, namespace):
        return self.subject_candidates

    def search_episode_embeddings(self, query_vector, *, limit=10, namespace=None):
        return self.search_hits[:limit]

    def list_scalar_history_views(self, *, subject_uuid, namespace=None):
        self.view_calls.append({"kind": "scalar", "namespace": namespace})
        return self.scalar_views

    def list_event_timeline_views(self, *, subject_uuid, namespace=None):
        self.view_calls.append({"kind": "event", "namespace": namespace})
        return self.event_views

    def list_scalar_history_entries(self, *, view_uuid, offset=0, limit=16, namespace=None):
        self.history_calls.append({"view_uuid": view_uuid, "offset": offset, "limit": limit,
                                   "namespace": namespace})
        entries = self.scalar_entries.get(view_uuid, [])
        page = entries[offset:offset + limit]
        end = offset + len(page)
        return {"entries": page, "total": len(entries),
                "next_offset": end if end < len(entries) else None}

    def list_event_timeline_entries(self, *, view_uuid, offset=0, limit=50, namespace=None):
        self.history_calls.append({"view_uuid": view_uuid, "offset": offset, "limit": limit,
                                   "namespace": namespace})
        return {"entries": [], "total": 0, "next_offset": None}

    def touch_retrieved_nodes(self, node_uuids):
        self.touched.extend(node_uuids)
        return len(node_uuids)


class _StubGraphiti:
    def __init__(self, hits: list[dict[str, Any]] | None = None) -> None:
        self.hits = hits or []
        self.embed_calls: list[str] = []

    async def embed_query(self, query: str) -> list[float]:
        self.embed_calls.append(query)
        return [0.1, 0.2]


def _svc(adapter: _StubAdapter, graphiti: _StubGraphiti | None = None) -> RecallService:
    return RecallService(
        graphiti_client=graphiti or _StubGraphiti(),
        graph_adapter=adapter,  # type: ignore[arg-type]
        scoring_service=ScoringService(),
    )


def _entries(result: TimelineResult) -> list[TimelineEntry]:
    return list(result.entries)


@pytest.mark.asyncio
async def test_tie_ordering_by_created_at_then_uuid() -> None:
    rows = [
        _row("ep-b", "2026-01-01", created_at="2026-01-01T02:00:00Z"),
        _row("ep-a", "2026-01-01", created_at="2026-01-01T02:00:00Z"),
        _row("ep-c", "2026-01-01", created_at="2026-01-01T01:00:00Z"),
    ]
    result = await _svc(_StubAdapter(rows)).recall_timeline(namespace="ns", at="2026-01-02")
    assert [e.uuid for e in result.entries] == ["ep-c", "ep-a", "ep-b"]


@pytest.mark.asyncio
async def test_at_split_both_with_inclusive_equal() -> None:
    rows = [
        _row("ep-1", "2026-01-01"),
        _row("ep-2", "2026-01-05"),
        _row("ep-3", "2026-01-05"),
        _row("ep-4", "2026-01-09"),
        _row("ep-5", "2026-01-10"),
    ]
    adapter = _StubAdapter(rows)
    result = await _svc(adapter).recall_timeline(
        namespace="ns", at="2026-01-05", limit=4
    )
    uuids = [e.uuid for e in result.entries]
    # limit//2 (2) at-or-before takes the two LATEST rows at or below the inclusive
    # boundary key (at, +inf, +inf) -- equal instants (ep-2, ep-3) are included -- and
    # the rest come after.
    assert uuids == ["ep-2", "ep-3", "ep-4", "ep-5"]
    assert result.prev_cursor is not None
    assert result.next_cursor is None  # the after side returned fewer than requested


@pytest.mark.asyncio
async def test_at_direction_before_single_side() -> None:
    rows = [_row(f"ep-{i}", f"2026-01-{i:02d}") for i in range(1, 6)]
    result = await _svc(_StubAdapter(rows)).recall_timeline(
        namespace="ns", at="2026-01-04", direction="before", limit=2
    )
    # The two latest rows at or below the inclusive boundary: ep-4 (== at) included.
    assert [e.uuid for e in result.entries] == ["ep-3", "ep-4"]
    assert result.next_cursor is not None
    assert result.prev_cursor is not None  # more rows exist before the page


@pytest.mark.asyncio
async def test_window_paging_and_next_cursor() -> None:
    rows = [_row(f"ep-{i}", f"2026-01-{i:02d}") for i in range(1, 6)]
    adapter = _StubAdapter(rows)
    result = await _svc(adapter).recall_timeline(
        namespace="ns", window_from="2026-01-01", limit=2
    )
    assert [e.uuid for e in result.entries] == ["ep-1", "ep-2"]
    assert result.next_cursor is not None

    decode_cursor(result.next_cursor, namespace_key="ns", subject_uuid=None)
    page2 = await _svc(adapter).recall_timeline(
        namespace="ns", cursor=result.next_cursor, limit=2
    )
    assert [e.uuid for e in page2.entries] == ["ep-3", "ep-4"]


@pytest.mark.asyncio
async def test_around_anchor_flag_and_neighbors() -> None:
    rows = [_row(f"ep-{i}", f"2026-01-{i:02d}") for i in range(1, 6)]
    result = await _svc(_StubAdapter(rows)).recall_timeline(
        namespace="ns", around="ep-3", limit=3
    )
    entries = result.entries
    assert [e.uuid for e in entries] == ["ep-2", "ep-3", "ep-4"]
    assert [e.is_anchor for e in entries] == [False, True, False]


@pytest.mark.asyncio
async def test_around_hidden_anchor_raises() -> None:
    adapter = _StubAdapter([_row("ep-1", "2026-01-01")])
    adapter.anchor_hidden.add("ep-1")
    with pytest.raises(ValueError, match="unknown or hidden memory"):
        await _svc(adapter).recall_timeline(namespace="ns", around="ep-1")


@pytest.mark.asyncio
async def test_query_mode_seeds_around() -> None:
    rows = [_row(f"ep-{i}", f"2026-01-{i:02d}") for i in range(1, 6)]
    adapter = _StubAdapter(rows)
    graphiti = _StubGraphiti()
    adapter.search_hits = [_row("ep-3", "2026-01-03")]
    result = await _svc(adapter, graphiti).recall_timeline(
        namespace="ns", query="auth", limit=3
    )
    assert graphiti.embed_calls == ["auth"]
    assert [e.uuid for e in result.entries] == ["ep-2", "ep-3", "ep-4"]
    assert [e.is_anchor for e in result.entries] == [False, True, False]


@pytest.mark.asyncio
async def test_query_mode_seed_search_filters_on_current_embedding_model() -> None:
    # T2: the seed search compares only vectors from the current embedder (#220).
    calls: list[dict[str, Any]] = []

    class _ModelAdapter(_StubAdapter):
        def search_episode_embeddings(self, query_vector, *, limit=10, namespace=None,
                                      model=None):
            calls.append({"limit": limit, "namespace": namespace, "model": model})
            return self.search_hits[:limit]

    class _Embedder:
        model = "text-embedding-3-small"

    graphiti = _StubGraphiti()
    graphiti.embedder_ref = _Embedder()  # type: ignore[attr-defined]
    adapter = _ModelAdapter([_row("ep-1", "2026-01-01")])
    adapter.search_hits = [_row("ep-1", "2026-01-01")]
    await _svc(adapter, graphiti).recall_timeline(namespace="ns", query="auth", limit=1)
    assert calls == [{"limit": 1, "namespace": "ns", "model": "text-embedding-3-small"}]


@pytest.mark.asyncio
async def test_query_mode_no_hits_empty_result_with_note() -> None:
    adapter = _StubAdapter([_row("ep-1", "2026-01-01")])
    result = await _svc(adapter, _StubGraphiti()).recall_timeline(namespace="ns", query="auth")
    assert result.entries == ()
    assert result.note is not None
    assert "No embedded memories matched" in result.note


@pytest.mark.asyncio
async def test_subject_only_latest_page() -> None:
    rows = [_row(f"ep-{i}", f"2026-01-{i:02d}") for i in range(1, 6)]
    adapter = _StubAdapter(rows)
    adapter.subject_candidates = [{"uuid": "entity-1", "name": "Rachel"}]
    result = await _svc(adapter).recall_timeline(namespace="ns", subject="rachel", limit=2)
    assert result.thread == "subject"
    assert result.subject_uuid == "entity-1"
    assert result.subject_name == "Rachel"
    assert [e.uuid for e in result.entries] == ["ep-4", "ep-5"]
    # The page probed before the max key, so the forward cursor stays available.
    assert result.next_cursor is not None


@pytest.mark.asyncio
async def test_subject_unknown_raises() -> None:
    adapter = _StubAdapter([])
    with pytest.raises(ValueError, match="unknown subject"):
        await _svc(adapter).recall_timeline(namespace="ns", subject="nobody")


@pytest.mark.asyncio
async def test_subject_ambiguous_raises_with_candidates() -> None:
    adapter = _StubAdapter([])
    adapter.subject_candidates = [
        {"uuid": "entity-1", "name": "Rachel A"},
        {"uuid": "entity-2", "name": "Rachel B"},
    ]
    with pytest.raises(ValueError, match="Rachel A \\(entity-1\\).*Rachel B \\(entity-2\\)"):
        await _svc(adapter).recall_timeline(namespace="ns", subject="rachel")


@pytest.mark.asyncio
async def test_exactly_one_start_required() -> None:
    adapter = _StubAdapter([])
    svc = _svc(adapter)
    with pytest.raises(ValueError):
        await svc.recall_timeline(namespace="ns")
    with pytest.raises(ValueError):
        await svc.recall_timeline(namespace="ns", at="2026-01-01", around="ep-1")
    with pytest.raises(ValueError):
        await svc.recall_timeline(namespace="ns", at="2026-01-01", cursor="x")


@pytest.mark.asyncio
async def test_subject_combines_with_at_and_around() -> None:
    rows = [_row(f"ep-{i}", f"2026-01-{i:02d}") for i in range(1, 6)]
    adapter = _StubAdapter(rows)
    adapter.subject_candidates = [{"uuid": "entity-1", "name": "Rachel"}]
    svc = _svc(adapter)
    at_result = await svc.recall_timeline(
        namespace="ns", subject="rachel", at="2026-01-03", limit=2
    )
    # limit 2 splits 1/1; the inclusive-equal boundary keeps ep-3 (== at) on the
    # before side (its epoch created_at sorts below +inf).
    assert [e.uuid for e in at_result.entries] == ["ep-3", "ep-4"]
    around_result = await svc.recall_timeline(
        namespace="ns", subject="rachel", around="ep-3", limit=3
    )
    assert [e.uuid for e in around_result.entries] == ["ep-2", "ep-3", "ep-4"]


@pytest.mark.asyncio
async def test_subject_with_cursor_pages_subject_thread() -> None:
    rows = [_row(f"ep-{i}", f"2026-01-{i:02d}") for i in range(1, 7)]
    adapter = _StubAdapter(rows)
    adapter.subject_candidates = [{"uuid": "entity-1", "name": "Rachel"}]
    svc = _svc(adapter)
    first = await svc.recall_timeline(
        namespace="ns", subject="rachel", window_from="2026-01-01", limit=2
    )
    assert [e.uuid for e in first.entries] == ["ep-1", "ep-2"]
    assert first.next_cursor is not None
    # Same subject: the cursor is accepted and pages the subject thread.
    page2 = await svc.recall_timeline(
        namespace="ns", subject="rachel", cursor=first.next_cursor, limit=2
    )
    assert [e.uuid for e in page2.entries] == ["ep-3", "ep-4"]
    # Without the subject the same cursor is rejected.
    with pytest.raises(ValueError):
        await svc.recall_timeline(namespace="ns", cursor=first.next_cursor, limit=2)


@pytest.mark.asyncio
async def test_page_order_preserved_for_string_timestamps() -> None:
    # Neo4j toString datetimes are not lexicographically ordered: the stub's string
    # sort puts "2026-01-01T00:01:30Z" before "2026-01-01T00:01Z". The service must
    # preserve the repository's order exactly, without re-sorting in Python.
    rows = [
        _row("ep-a", "2026-01-01T00:01Z"),
        _row("ep-b", "2026-01-01T00:01:30Z"),
    ]
    adapter = _StubAdapter(rows)
    result = await _svc(adapter).recall_timeline(
        namespace="ns", window_from="2026-01-01T00:00Z", limit=5
    )
    assert [e.uuid for e in result.entries] == ["ep-b", "ep-a"]


@pytest.mark.asyncio
async def test_invalid_iso_instant_raises() -> None:
    with pytest.raises(ValueError, match="valid ISO instant"):
        await _svc(_StubAdapter([])).recall_timeline(namespace="ns", at="not-a-date")
    with pytest.raises(ValueError, match="valid ISO instant"):
        await _svc(_StubAdapter([])).recall_timeline(
            namespace="ns", window_from="2026-13-45")


@pytest.mark.asyncio
async def test_limit_and_history_limit_clamped() -> None:
    adapter = _StubAdapter([])
    adapter.subject_candidates = [{"uuid": "entity-1", "name": "Rachel"}]
    svc = _svc(adapter)
    await svc.recall_timeline(namespace="ns", at="2026-01-01", limit=999)
    # limit clamped to 50, split 25/25 across the two sides, each probed +1 row.
    assert max(c["limit"] for c in adapter.page_calls) == 26
    await svc.recall_timeline(namespace="ns", at="2026-01-01", limit=-5)
    assert min(c["limit"] for c in adapter.page_calls) == 2  # clamped to 1, +1 probe row


@pytest.mark.asyncio
async def test_detail_full_content_cap_and_headline_cap() -> None:
    long = "a b  " + ("x" * 5000)
    adapter = _StubAdapter([_row("ep-1", "2026-01-01", content=long)])
    result = await _svc(adapter).recall_timeline(namespace="ns", at="2026-01-02",
                                                 detail="full")
    entry = result.entries[0]
    assert len(entry.headline) == 161 and entry.headline.endswith("…")
    assert entry.content is not None
    assert len(entry.content) == 4001 and entry.content.endswith("…")

    headline_only = await _svc(_StubAdapter([_row("ep-1", "2026-01-01", content=long)])
                               ).recall_timeline(namespace="ns", at="2026-01-02")
    assert headline_only.entries[0].content is None


@pytest.mark.asyncio
async def test_facts_true_attaches_facts() -> None:
    adapter = _StubAdapter([_row("ep-1", "2026-01-01")])
    adapter.facts = {"ep-1": [{
        "fact": "Rachel moved to Austin", "valid_at": "2026-01-01",
        "invalid_at": None, "expired_at": None,
    }]}
    result = await _svc(adapter).recall_timeline(namespace="ns", at="2026-01-02", facts=True)
    assert adapter.facts_calls, "facts call missing"
    entry = result.entries[0]
    assert len(entry.facts) == 1
    assert entry.facts[0].fact == "Rachel moved to Austin"
    assert entry.facts[0].time_basis == "world"

    no_facts = await _svc(_StubAdapter([_row("ep-1", "2026-01-01")])
                          ).recall_timeline(namespace="ns", at="2026-01-02")
    assert no_facts.entries[0].facts == ()


def _scalar_entry(ordinal: int) -> dict[str, Any]:
    return {
        "assertion_id": f"a-{ordinal}", "ordinal": ordinal, "operation": "SET",
        "value": str(ordinal), "stated_span": f"quote {ordinal}",
        "valid_at": f"2026-01-{ordinal:02d}", "episode_uuid": f"ep-{ordinal}",
    }


@pytest.mark.asyncio
async def test_typed_histories_default_to_latest_page() -> None:
    adapter = _StubAdapter([])
    adapter.subject_candidates = [{"uuid": "entity-1", "name": "Rachel"}]
    adapter.scalar_views = [{"uuid": "view-1", "attribute": "version"}]
    adapter.scalar_entries["view-1"] = [_scalar_entry(i) for i in range(1, 8)]
    result = await _svc(adapter).recall_timeline(
        namespace="ns", subject="Rachel", history_limit=3
    )
    assert len(result.histories) == 1
    history = result.histories[0]
    assert history.kind == "scalar" and history.label == "version"
    assert history.total == 7
    assert history.offset == 4  # latest 3 of 7
    assert [e.value for e in history.entries] == ["5", "6", "7"]
    assert history.prev_offset == 1 and history.next_offset is None
    calls = [c for c in adapter.history_calls if c["view_uuid"] == "view-1"]
    assert calls[-1]["offset"] == 4 and calls[-1]["limit"] == 3


@pytest.mark.asyncio
async def test_typed_history_view_and_offset_paging() -> None:
    adapter = _StubAdapter([])
    adapter.subject_candidates = [{"uuid": "entity-1", "name": "Rachel"}]
    adapter.scalar_views = [{"uuid": "view-1", "attribute": "version"}]
    adapter.scalar_entries["view-1"] = [_scalar_entry(i) for i in range(1, 8)]
    result = await _svc(adapter).recall_timeline(
        namespace="ns", subject="Rachel", history_view="view-1",
        history_offset=0, history_limit=3,
    )
    history = result.histories[0]
    assert history.offset == 0
    assert [e.value for e in history.entries] == ["1", "2", "3"]
    assert history.next_offset == 3 and history.prev_offset is None
    assert [e.quote for e in history.entries] == [
        "quote 1", "quote 2", "quote 3"]


@pytest.mark.asyncio
async def test_typed_histories_capped_at_ten_with_note() -> None:
    adapter = _StubAdapter([])
    adapter.subject_candidates = [{"uuid": "entity-1", "name": "Rachel"}]
    adapter.scalar_views = [
        {"uuid": f"view-{i}", "attribute": f"attr-{i}"} for i in range(12)
    ]
    result = await _svc(adapter).recall_timeline(namespace="ns", subject="Rachel")
    assert len(result.histories) == 10
    assert result.note is not None and "2 more" in result.note


@pytest.mark.asyncio
async def test_typed_histories_gid_for_default_namespace() -> None:
    adapter = _StubAdapter([])
    adapter.subject_candidates = [{"uuid": "entity-1", "name": "Rachel"}]
    await _svc(adapter).recall_timeline(namespace="default", subject="Rachel")
    assert adapter.view_calls[0]["namespace"] == ""
    await _svc(adapter).recall_timeline(namespace=None, subject="Rachel")
    assert adapter.view_calls[-1]["namespace"] is None


@pytest.mark.asyncio
async def test_no_access_update_calls() -> None:
    adapter = _StubAdapter([_row("ep-1", "2026-01-01")])
    result = await _svc(adapter).recall_timeline(namespace="ns", at="2026-01-02")
    assert result.entries
    assert adapter.touched == []


@pytest.mark.asyncio
async def test_result_is_frozen_dataclass() -> None:
    result = await _svc(_StubAdapter([_row("ep-1", "2026-01-01")])).recall_timeline(
        namespace="ns", at="2026-01-02"
    )
    assert isinstance(result, TimelineResult)
    with pytest.raises(Exception):
        result.entries = ()  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Phase B: REST, MCP, registries (pattern: tests/test_recall_history.py)
# ---------------------------------------------------------------------------


def test_recall_timeline_request_bounds() -> None:
    from pydantic import ValidationError

    from menhir.api.routes_support import RecallTimelineRequest

    req = RecallTimelineRequest()
    assert req.limit == 10 and req.direction == "both" and req.detail == "headline"
    assert req.namespace is None and req.subject is None and req.facts is False
    assert RecallTimelineRequest(limit=1).limit == 1
    assert RecallTimelineRequest(limit=50).limit == 50
    assert RecallTimelineRequest(history_limit=50).history_limit == 50
    with pytest.raises(ValidationError):
        RecallTimelineRequest(limit=51)
    with pytest.raises(ValidationError):
        RecallTimelineRequest(limit=0)
    with pytest.raises(ValidationError):
        RecallTimelineRequest(direction="sideways")
    with pytest.raises(ValidationError):
        RecallTimelineRequest(detail="verbose")
    with pytest.raises(ValidationError):
        RecallTimelineRequest(history_limit=51)


def test_rest_invalid_timeline_start_returns_422() -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from menhir.api import routes as api_routes
    from menhir.api.routes import router

    backend = SimpleNamespace()
    backend.recall_timeline = AsyncMock(
        side_effect=ValueError("unknown subject")
    )
    app = FastAPI()
    app.include_router(router)
    with patch.object(api_routes, "_get_backend", return_value=backend):
        resp = TestClient(app).post("/api/recall/timeline", json={"subject": "ghost"})
    assert resp.status_code == 422
    assert "unknown subject" in resp.json()["detail"]


def test_rest_timeline_response_shape_and_note_omission() -> None:
    from menhir.api.routes_support import RecallTimelineResponse

    payload = RecallTimelineResponse(
        thread="namespace",
        entries=[{
            "uuid": "ep-1",
            "recorded_at": "2026-01-01T00:00:00Z",
            "created_at": None,
            "session_id": "s1",
            "source": "claude-code",
            "headline": "did the thing",
            "content": None,
            "facts": [],
            "is_anchor": True,
        }],
        prev_cursor=None,
        next_cursor="tok",
        histories=[],
    ).model_dump(exclude_none=True)
    assert payload["entries"][0]["uuid"] == "ep-1"
    assert payload["entries"][0]["is_anchor"] is True
    assert "content" not in payload["entries"][0]
    assert "note" not in payload

    empty = RecallTimelineResponse(thread="namespace", entries=[], note="nothing")
    assert empty.note == "nothing"


class _TimelineBackend:
    def __init__(self, result: dict[str, Any]) -> None:
        self._result = result

    async def recall_timeline(self, **kwargs: Any) -> dict[str, Any]:
        return self._result


@pytest.mark.asyncio
async def test_mcp_timeline_payload_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    from menhir.mcp.tools.recall.recall_timeline import RecallTimelineTool

    result = {
        "thread": "subject",
        "subject_uuid": "entity-1",
        "subject_name": "Rachel",
        "entries": [
            {
                "uuid": "ep-1",
                "recorded_at": "2026-01-01T00:00:00Z",
                "created_at": None,
                "session_id": "s1",
                "source": "claude-code",
                "headline": "did the thing",
                "content": None,
                "facts": [{"fact": "f", "valid_at": "2026-01-01",
                           "invalid_at": None, "expired_at": None,
                           "time_basis": "world"}],
                "is_anchor": False,
            },
            {
                "uuid": "ep-2",
                "recorded_at": "2026-02-01T00:00:00Z",
                "created_at": None,
                "session_id": "s1",
                "source": None,
                "headline": "anchor entry",
                "content": "full text",
                "facts": [],
                "is_anchor": True,
            },
        ],
        "prev_cursor": "prev-tok",
        "next_cursor": "next-tok",
        "histories": [{
            "kind": "scalar",
            "view_uuid": "view-1",
            "label": "owned",
            "total": 2,
            "offset": 0,
            "entries": [{"valid_at": "2026-01-01", "value": 20, "operation": "absolute",
                         "time_basis": None, "quote": "I owned 20", "episode_uuid": "ep-1"}],
            "prev_offset": None,
            "next_offset": 1,
        }],
    }
    tool = RecallTimelineTool()
    monkeypatch.setattr(tool, "get_backend", lambda: _TimelineBackend(result))
    raw = await tool.endpoint(subject="Rachel")
    payload = json.loads(raw)
    assert payload["thread"] == "subject"
    assert payload["count"] == 2
    assert payload["subject"] == {"uuid": "entity-1", "name": "Rachel"}
    assert payload["entries"][0]["recorded_at"] == "2026-01-01T00:00:00Z"
    assert payload["entries"][0]["headline"] == "did the thing"
    assert "content" not in payload["entries"][0]
    assert payload["entries"][0]["facts"][0]["fact"] == "f"
    assert payload["entries"][1]["is_anchor"] is True
    assert payload["entries"][1]["content"] == "full text"
    assert payload["prev_cursor"] == "prev-tok"
    assert payload["next_cursor"] == "next-tok"
    assert payload["histories"][0]["label"] == "owned"
    assert payload["histories"][0]["entries"][0]["value"] == 20
    assert "note" not in payload


@pytest.mark.asyncio
async def test_mcp_timeline_note_present_when_nothing_matched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    from menhir.mcp.tools.recall.recall_timeline import RecallTimelineTool

    result = {
        "thread": "namespace",
        "subject_uuid": None,
        "subject_name": None,
        "entries": [],
        "prev_cursor": None,
        "next_cursor": None,
        "histories": [],
        "note": "No embedded memories matched, so there is no timeline to anchor on.",
    }
    tool = RecallTimelineTool()
    monkeypatch.setattr(tool, "get_backend", lambda: _TimelineBackend(result))
    payload = json.loads(await tool.endpoint(query="q"))
    assert payload["count"] == 0
    assert "no timeline to anchor on" in payload["note"]


@pytest.mark.asyncio
async def test_mcp_timeline_value_error_returns_error_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    from menhir.mcp.tools.recall.recall_timeline import RecallTimelineTool

    backend = _TimelineBackend({})

    async def _raise(**kwargs: Any) -> dict[str, Any]:
        raise ValueError("exactly one of at / window_from+window_to / around / "
                         "cursor / query is required")

    backend.recall_timeline = _raise  # type: ignore[method-assign]
    tool = RecallTimelineTool()
    monkeypatch.setattr(tool, "get_backend", lambda: backend)
    payload = json.loads(await tool.endpoint(at="2026-01-01", around="ep-1"))
    assert payload["ok"] is False
    assert payload["tool"] == "recall_timeline"
    assert "exactly one" in payload["error"]["message"]


def test_registered_in_recall_tools() -> None:
    from menhir.mcp.tools.recall import RECALL_TOOLS

    assert any(getattr(t, "name", "") == "recall_timeline" for t in RECALL_TOOLS)


def test_registered_but_not_always_visible() -> None:
    import asyncio

    from menhir.mcp import server as mcp_server

    registered = {t.name for t in asyncio.run(mcp_server.mcp._list_tools())}
    assert "recall_timeline" in registered
    visible = {t.name for t in asyncio.run(mcp_server.mcp.list_tools())}
    assert "recall_timeline" not in visible


def test_tool_policy_attributes() -> None:
    from menhir.mcp.contracts import ToolScope
    from menhir.mcp.tools.recall.recall_timeline import RecallTimelineTool

    tool = RecallTimelineTool()
    assert tool.scope == ToolScope.NAMESPACED
    assert tool.required_tier == "readonly"
    assert tool.oauth_scopes == ("menhir:read",)
    assert tool.read_only_hint is True
    assert tool.destructive_hint is False


def test_in_agent_allowed_tools() -> None:
    from menhir.access_contract import AGENT_ALLOWED_TOOLS

    assert "recall_timeline" in AGENT_ALLOWED_TOOLS


def test_in_feature_taxonomy() -> None:
    from menhir.explorer.feature_taxonomy import PARENTS

    assert "recall_timeline" in PARENTS["retrieve"]


def test_in_ratable_operations() -> None:
    from menhir.mcp.feedback import RATABLE_OPERATIONS

    assert "recall_timeline" in RATABLE_OPERATIONS


def test_in_backend_methods() -> None:
    from menhir.api.routes_support import _BACKEND_METHODS

    assert "recall_timeline" in _BACKEND_METHODS


# ---------------------------------------------------------------------------
# Phase B: brief timeline removal
# ---------------------------------------------------------------------------


def test_memory_settings_has_no_frontier_brief_builder() -> None:
    import dataclasses

    from menhir.config.settings_model import MemorySettings

    names = {f.name for f in dataclasses.fields(MemorySettings)}
    assert "frontier_brief_builder" not in names


def test_brief_builder_module_is_gone() -> None:
    import importlib.util

    assert importlib.util.find_spec("menhir.domain.brief_builder") is None
    from menhir.services.context_builder import ContextBuilderService

    assert not hasattr(ContextBuilderService, "brief_builder_enabled")


@pytest.mark.asyncio
async def test_build_context_never_contains_brief_timeline() -> None:
    from types import SimpleNamespace

    from menhir.domain.recall import RecallResult
    from menhir.services.context_builder import ContextBuilderService

    recall_calls: list[dict[str, Any]] = []

    async def _recall(*args: Any, **kwargs: Any) -> RecallResult:
        recall_calls.append(kwargs)
        return RecallResult(
            query="q", preset="knowledge", results=(), candidates_evaluated=0,
            nodes_touched=0,
        )

    builder = ContextBuilderService(recall_service=SimpleNamespace(recall=_recall))
    result = await builder.build_context("q", max_tokens=2000)
    assert "=== Timeline ===" not in result.context
    assert recall_calls[0].get("include_invalidated") is True


def test_timeline_reads_do_not_require_processing_state() -> None:
    # Live check (AMA test graph): Graphiti's resolved :Episodic nodes -- the ones carrying
    # valid_at, MENTIONS and fact ids -- need not carry processing_state (only Menhir's queue
    # node is guaranteed to), so requiring it emptied the whole timeline. FAILED stays excluded.
    for call in ("page", "anchor"):
        repository, neo4j = _repository()
        if call == "page":
            repository.timeline_page(namespace="tenant-a", limit=5)
        else:
            repository.timeline_anchor(uuid="ep-1", namespace="tenant-a")
        query, _params = neo4j.calls[0]
        assert "n.processing_state IS NOT NULL" not in query
        assert "coalesce(n.processing_state, '') <> 'FAILED'" in query
        assert "n.valid_at IS NOT NULL" in query


@pytest.mark.asyncio
async def test_at_accepts_neo4j_zoned_instant_from_own_output() -> None:
    # recorded_at comes back in Neo4j's `...Z[UTC]` form; passing it back as `at` must work.
    rows = [_row("ep-1", "2026-01-01"), _row("ep-2", "2026-01-02")]
    result = await _svc(_StubAdapter(rows)).recall_timeline(
        namespace="ns", at="2026-01-01T01:20:00Z[UTC]"
    )
    assert [e.uuid for e in result.entries]
    with pytest.raises(ValueError):
        await _svc(_StubAdapter(rows)).recall_timeline(namespace="ns", at="not-a-time[UTC]")


def test_timeline_hides_episodes_whose_memory_receipt_is_gone() -> None:
    # Audit Q1: erasing/deleting a memory by its receipt (queue-node) uuid leaves Graphiti's
    # episode with the raw text; the timeline must only show episodes a visible queue node
    # still resolves to -- on pages and on `around` anchors alike.
    for call in ("page", "anchor"):
        repository, neo4j = _repository()
        if call == "page":
            repository.timeline_page(namespace="tenant-a", limit=5)
        else:
            repository.timeline_anchor(uuid="ep-1", namespace="tenant-a")
        query, _params = neo4j.calls[0]
        assert "EXISTS { MATCH (q:Episodic) WHERE q.resolved_episode_uuid = n.uuid AND" in query
