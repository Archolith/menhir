import json
import asyncio
from datetime import datetime, timezone
from uuid import UUID
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from menhir.mcp import formatters
from menhir.mcp.formatters import (
    _coerce_iso,
    _parse_graph_datetime,
    _normalize_reader_id,
    _require_episode_uuid,
    _compact_json,
    _resolve_queue_state_filter,
    _resolve_conflict_status_filter,
    _node_sort_key,
    _coerce_conflict_members,
    _count_unresolved_members,
    _stale_reason_for_row,
    _compact_memory_item,
    _compact_scored_item,
    _format_episode_status,
    _format_row_memory,
    _format_episode_watch,
    _collect_episode_status,
    _queue_summary,
)
from menhir.domain.models import ProcessingState


@pytest.fixture(autouse=True)
def _isolate_stuck_count_cache():
    """`_standing_unrecallable_count` memoizes into a MODULE-GLOBAL with a 60s TTL.

    That is deliberate -- `add_memory` is a hot path -- but it means a test that leaves a count in it
    changes what a LATER test in a DIFFERENT file observes. This module's mock backends do not answer
    `fetch_memory_overview` with real data, so before the explicit configuration below they published
    a fabricated count that made `test_mcp_server.py::test_add_memory_queues_episode_for_background_
    enrichment` fail on a FAILED-memory warning it never triggered -- and pass in isolation.

    Reset on both sides: entering, so an earlier module cannot bias these assertions, and leaving, so
    these cannot bias anything after them.
    """
    formatters._stuck_count_cache = None
    yield
    formatters._stuck_count_cache = None


def test_coerce_iso():
    assert _coerce_iso(None) is None

    dt = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    assert _coerce_iso(dt) == "2024-01-01T12:00:00+00:00"

    assert _coerce_iso("  2024-01-01  ") == "2024-01-01"
    assert _coerce_iso("") is None
    assert _coerce_iso("   ") is None

    class MockDate:
        def isoformat(self):
            return "mock-iso"
    assert _coerce_iso(MockDate()) == "mock-iso"


def test_parse_graph_datetime():
    assert _parse_graph_datetime(None) is None
    assert _parse_graph_datetime("") is None

    val = "2024-01-01T12:00:00Z"
    parsed = _parse_graph_datetime(val)
    assert parsed == datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    val2 = "2024-01-01T12:00:00"
    parsed2 = _parse_graph_datetime(val2)
    assert parsed2 == datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    assert _parse_graph_datetime("not-a-date") is None

    dt = datetime(2024, 2, 2, 10, 0, 0, tzinfo=timezone.utc)
    assert _parse_graph_datetime(dt) == dt


def test_normalize_reader_id():
    assert _normalize_reader_id(None) == "default"
    assert _normalize_reader_id("") == "default"
    assert _normalize_reader_id("   ") == "default"
    assert _normalize_reader_id("  user-123  ") == "user-123"


def test_require_episode_uuid():
    valid_uuid = "123e4567-e89b-12d3-a456-426614174000"
    assert _require_episode_uuid(valid_uuid) == valid_uuid
    assert _require_episode_uuid(f"  {valid_uuid}  ") == valid_uuid

    with pytest.raises(ValueError, match="Invalid episode UUID"):
        _require_episode_uuid("not-a-uuid")

    with pytest.raises(ValueError, match="Invalid episode UUID"):
        _require_episode_uuid(None)


def test_compact_json():
    data = {"a": 1, "b": [2, 3], "c": "val"}
    compacted = _compact_json(data)
    assert compacted == '{"a":1,"b":[2,3],"c":"val"}'

    data_with_uuid = {"id": UUID("123e4567-e89b-12d3-a456-426614174000")}
    assert '"id":"123e4567-e89b-12d3-a456-426614174000"' in _compact_json(data_with_uuid)


def test_resolve_queue_state_filter():
    assert _resolve_queue_state_filter(None) == ("ACTIVE", ["PENDING", "ENRICHING"])
    assert _resolve_queue_state_filter("  active  ") == ("ACTIVE", ["PENDING", "ENRICHING"])
    assert _resolve_queue_state_filter("QUEUED") == ("ACTIVE", ["PENDING", "ENRICHING"])
    assert _resolve_queue_state_filter("all") == ("ALL", None)
    assert _resolve_queue_state_filter("pending") == ("PENDING", ["PENDING"])
    assert _resolve_queue_state_filter("enriching") == ("ENRICHING", ["ENRICHING"])
    assert _resolve_queue_state_filter("ready") == ("READY", ["READY"])
    assert _resolve_queue_state_filter("failed") == ("FAILED", ["FAILED"])

    with pytest.raises(ValueError, match="Invalid state"):
        _resolve_queue_state_filter("unknown")


def test_resolve_conflict_status_filter():
    assert _resolve_conflict_status_filter(None) == ("UNRESOLVED", "unresolved")
    assert _resolve_conflict_status_filter("  all  ") == ("ALL", None)
    assert _resolve_conflict_status_filter("resolved") == ("RESOLVED", "resolved")
    assert _resolve_conflict_status_filter("auto-resolved") == ("AUTO-RESOLVED", "auto-resolved")

    with pytest.raises(ValueError, match="Invalid status"):
        _resolve_conflict_status_filter("invalid")


def test_node_sort_key():
    member1 = {"node_created_at": "2024-01-01T10:00:00Z", "uuid": "uuid1"}
    assert _node_sort_key(member1) == (0, "2024-01-01T10:00:00+00:00")

    member2 = {"uuid": "uuid2"}
    assert _node_sort_key(member2) == (1, "uuid2")

    member3 = {}
    assert _node_sort_key(member3) == (1, "")


def test_coerce_conflict_members():
    row = {
        "members": [
            {"uuid": "b", "node_created_at": "2024-01-01T12:00:00Z"},
            {"uuid": "a", "node_created_at": "2024-01-01T10:00:00Z"},
        ]
    }
    coerced = _coerce_conflict_members(row)
    assert len(coerced) == 2
    assert coerced[0]["uuid"] == "a"
    assert coerced[1]["uuid"] == "b"

    assert _coerce_conflict_members({}) == []
    assert _coerce_conflict_members({"members": "not-a-list"}) == []


def test_count_unresolved_members():
    assert _count_unresolved_members(None) == 0

    row = {
        "members": [
            {"status": "unresolved", "uuid": "1"},
            {"status": "resolved", "uuid": "2"},
            {"status": "unresolved", "uuid": "3"},
        ]
    }
    assert _count_unresolved_members(row) == 2


def test_stale_reason_for_row():
    now = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    assert _stale_reason_for_row({"processing_state": "PENDING"}, now_utc=now) is None

    row = {"processing_state": "ENRICHING"}
    assert _stale_reason_for_row(row, now_utc=now) == "missing_lease"

    row = {
        "processing_state": "ENRICHING",
        "processing_lease_expires_at": "2024-01-01T11:59:59Z"
    }
    assert _stale_reason_for_row(row, now_utc=now) == "lease_expired"

    row = {
        "processing_state": "ENRICHING",
        "processing_lease_expires_at": "2024-01-01T12:00:01Z",
        "processing_owner": None
    }
    assert _stale_reason_for_row(row, now_utc=now) == "missing_owner"

    row = {
        "processing_state": "ENRICHING",
        "processing_lease_expires_at": "2024-01-01T12:00:01Z",
        "processing_owner": "worker-1"
    }
    assert _stale_reason_for_row(row, now_utc=now) is None


def test_compact_memory_item():
    row = {
        "uuid": "u1",
        "name": "name1",
        "scope": "PERSISTENT",
        "type": "semantic",
        "summary": "long summary here",
        "similarity": 0.95
    }
    compacted = _compact_memory_item(row, tag="mytag")
    assert compacted["uuid"] == "u1"
    assert compacted["tag"] == "mytag"
    assert compacted["similarity"] == 0.95
    assert compacted["name"] == "name1"

    row2 = {"uuid": "u2"}
    compacted2 = _compact_memory_item(row2, tag="tag2")
    assert compacted2["name"] == "u2"
    assert compacted2["scope"] == "UNKNOWN"
    assert compacted2["type"] == "UNKNOWN"


def test_compact_scored_item():
    scored = SimpleNamespace(
        uuid="u1",
        name="name1",
        scope="SESSION",
        memory_type="episodic",
        final_score=0.8888,
        summary="sum",
        breakdown=SimpleNamespace(
            semantic_similarity=0.7777,
            adjacency_bonus=0.1,
            recency_bonus=0.01,
            prominence_bonus=0.001
        )
    )
    compacted = _compact_scored_item(scored)
    assert compacted["uuid"] == "u1"
    assert compacted["score"] == 0.889
    assert compacted["relevance"] == "high"
    assert compacted["breakdown"]["sim"] == 0.778
    assert compacted["breakdown"]["adj"] == 0.1
    assert compacted["breakdown"]["rec"] == 0.01
    assert compacted["breakdown"]["prom"] == 0.001
    # a plain candidate carries no scalar-authority marker
    assert "is_scalar_authority" not in compacted


def test_compact_scored_item_marks_scalar_authority():
    # Phase 4a.4/4c: the deterministically-injected current View is marked so the consumer leads with it.
    scored = SimpleNamespace(
        uuid="v1", name="user's owned: 37. current owned = 37.", scope="PERSISTENT",
        memory_type="SCALAR_STATE", final_score=0.9, summary="owned=37",
        is_scalar_authority=True,
        breakdown=SimpleNamespace(semantic_similarity=0.9, adjacency_bonus=0.0,
                                  recency_bonus=0.0, prominence_bonus=0.0),
    )
    for compact in (False, True):
        item = _compact_scored_item(scored, compact=compact)
        assert item["is_scalar_authority"] is True   # kept even in compact mode (decision-relevant)


def test_compact_scored_item_compact_mode_drops_diagnostics():
    """compact=True drops breakdown + type, keeps decision-relevant fields."""
    scored = SimpleNamespace(
        uuid="u1",
        name="name1",
        scope="PERSISTENT",
        memory_type="SEMANTIC",
        final_score=0.8888,
        summary="sum",
        breakdown=SimpleNamespace(
            semantic_similarity=0.7777,
            adjacency_bonus=0.1,
            recency_bonus=0.01,
            prominence_bonus=0.001,
        ),
    )
    compacted = _compact_scored_item(scored, compact=True)
    # decision-relevant fields retained
    assert compacted["uuid"] == "u1"
    assert compacted["name"] == "name1"
    assert compacted["scope"] == "PERSISTENT"
    assert compacted["score"] == 0.889
    assert compacted["relevance"] == "high"
    assert compacted["summary"] == "sum"
    # diagnostic fields dropped
    assert "breakdown" not in compacted
    assert "type" not in compacted


def test_compact_scored_item_dict_breakdown():
    """When breakdown is a plain dict (from _to_jsonable/JSON round-trip), values must still propagate."""
    scored = SimpleNamespace(
        uuid="u2",
        name="name2",
        scope="PERSISTENT",
        memory_type="SEMANTIC",
        final_score=1.2345,
        summary="dict breakdown test",
        breakdown={
            "semantic_similarity": 0.6543,
            "adjacency_bonus": 0.2,
            "recency_bonus": 0.05,
            "prominence_bonus": 0.003,
        },
    )
    compacted = _compact_scored_item(scored)
    assert compacted["breakdown"]["sim"] == 0.654
    assert compacted["breakdown"]["adj"] == 0.2
    assert compacted["breakdown"]["rec"] == 0.05
    assert compacted["breakdown"]["prom"] == 0.003


def test_compact_scored_item_none_breakdown():
    """When breakdown is None, all breakdown values should be 0.0."""
    scored = SimpleNamespace(
        uuid="u3",
        name="name3",
        scope="PERSISTENT",
        memory_type="SEMANTIC",
        final_score=0.5,
        summary="no breakdown",
        breakdown=None,
    )
    compacted = _compact_scored_item(scored)
    assert compacted["breakdown"]["sim"] == 0.0
    assert compacted["breakdown"]["adj"] == 0.0
    assert compacted["breakdown"]["rec"] == 0.0
    assert compacted["breakdown"]["prom"] == 0.0


def test_compact_scored_item_relevance_tiers():
    """Relevance tier is based on semantic_similarity: high >= 0.7, medium >= 0.4, low < 0.4."""
    high = SimpleNamespace(
        uuid="h", name="h", scope="S", memory_type="T", final_score=1.0, summary="",
        breakdown={"semantic_similarity": 0.85, "adjacency_bonus": 0, "recency_bonus": 0, "prominence_bonus": 0},
    )
    medium = SimpleNamespace(
        uuid="m", name="m", scope="S", memory_type="T", final_score=0.6, summary="",
        breakdown={"semantic_similarity": 0.5, "adjacency_bonus": 0, "recency_bonus": 0, "prominence_bonus": 0},
    )
    low = SimpleNamespace(
        uuid="l", name="l", scope="S", memory_type="T", final_score=0.2, summary="",
        breakdown={"semantic_similarity": 0.15, "adjacency_bonus": 0, "recency_bonus": 0, "prominence_bonus": 0},
    )
    assert _compact_scored_item(high)["relevance"] == "high"
    assert _compact_scored_item(medium)["relevance"] == "medium"
    assert _compact_scored_item(low)["relevance"] == "low"


def test_format_episode_status():
    assert "status: not_found" in _format_episode_status(episode_uuid="e1", row=None, history=[], timed_out=False)

    row = {
        "processing_state": "READY",
        "processing_stage": "stage1",
        "processing_progress": 50.55,
        "processing_steps_completed": 5,
        "processing_steps_total": 10
    }
    history = [
        {"state": "PENDING", "attempts": 1, "queue_depth": 5, "processing_error": None}
    ]
    formatted = _format_episode_status(episode_uuid="e1", row=row, history=history, timed_out=True)
    assert "episode_id: e1" in formatted
    assert "status: READY" in formatted
    assert "progress: 50.5" in formatted
    assert "steps: 5/10" in formatted
    assert "timed_out: True" in formatted
    assert "history:" in formatted
    assert "state=PENDING" in formatted


def test_format_row_memory():
    row = {
        "uuid": "u1",
        "name": "n1",
        "scope": "SESSION",
        "type": "t1",
        "content": "some content"
    }
    formatted = _format_row_memory(1, row, tag="tag1")
    assert "[1] n1 [tag1]" in formatted
    assert "uuid: u1" in formatted
    assert "scope: SESSION | type: t1" in formatted
    assert "content: some content" in formatted


def test_format_episode_watch():
    formatted = _format_episode_watch(episode_uuid="e1", row={"processing_state": "PENDING"}, history=[], timed_out=False)
    assert "(no updates observed)" in formatted

    history = [
        {
            "state": "ENRICHING",
            "attempts": 1,
            "queue_depth": 2,
            "processing_error": "err",
            "llm_active_task": "task1",
            "llm_active_endpoint": "end1",
            "heartbeat_at": "2024-01-01"
        }
    ]
    formatted2 = _format_episode_watch(episode_uuid="e1", row={"processing_state": "ENRICHING"}, history=history, timed_out=False)
    assert "deltas:" in formatted2
    assert "state=ENRICHING" in formatted2
    assert "llm_active_task=task1" in formatted2
    assert "heartbeat_at=2024-01-01" in formatted2


def test_queue_summary():
    async def _test():
        backend1 = AsyncMock()
        backend1.get_queue_depth.return_value = 10
        backend1.list_episode_processing.return_value = [{"uuid": "1"}, {"uuid": "2"}]
        backend1.scheduler_status_snapshot.return_value = {"running": True}
        # Answer the backlog query explicitly. Left unconfigured, a MagicMock satisfies `int(...)`
        # with 1, so this queue-depth test would cache "1 FAILED memory" for every later caller.
        backend1.fetch_memory_overview.return_value = {"failed_count": 0}

        summary1 = await _queue_summary(backend1)
        assert "queue_depth=10" in summary1
        assert "active_enriching=2" in summary1
        assert "scheduler=running" in summary1
        assert "WARNING" not in summary1

        formatters._stuck_count_cache = None    # the second backend must be measured, not cached

        backend2 = MagicMock(spec=["ingest_service", "graph_adapter", "scheduler"])
        backend2.ingest_service.get_queue_depth.return_value = 5
        backend2.graph_adapter.list_episode_processing.return_value = []
        backend2.graph_adapter.fetch_memory_overview.return_value = {"failed_count": 0}
        backend2.scheduler.status_snapshot.return_value = {"running": False}

        summary2 = await _queue_summary(backend2)
        assert "queue_depth=5" in summary2
        assert "active_enriching=0" in summary2
        assert "scheduler=stopped" in summary2
        assert "WARNING" not in summary2

    asyncio.run(_test())


def test_queue_summary_leaves_no_fabricated_stuck_count_behind():
    """Order regression for the module-global cache.

    `_queue_summary` always asks for the standing FAILED-episode count, on BOTH backend shapes. A
    mock that does not answer that question still returns SOMETHING, and the count it fabricates is
    published to every writer for the next 60 seconds -- which is how a formatter test broke an
    `add_memory` assertion in another file. What lands in the cache must be what the backend said.
    """
    async def _test():
        for backend, expected in (
            (_async_backend(failed_count=0), 0),
            (_sync_backend(failed_count=7), 7),
        ):
            formatters._stuck_count_cache = None
            await _queue_summary(backend)
            assert formatters._stuck_count_cache is not None
            assert formatters._stuck_count_cache[1] == expected

    asyncio.run(_test())


def _async_backend(*, failed_count: int) -> AsyncMock:
    backend = AsyncMock()
    backend.get_queue_depth.return_value = 0
    backend.list_episode_processing.return_value = []
    backend.scheduler_status_snapshot.return_value = {"running": True}
    backend.fetch_memory_overview.return_value = {"failed_count": failed_count}
    return backend


def _sync_backend(*, failed_count: int) -> MagicMock:
    backend = MagicMock(spec=["ingest_service", "graph_adapter", "scheduler"])
    backend.ingest_service.get_queue_depth.return_value = 0
    backend.graph_adapter.list_episode_processing.return_value = []
    backend.graph_adapter.fetch_memory_overview.return_value = {"failed_count": failed_count}
    backend.scheduler.status_snapshot.return_value = {"running": True}
    return backend


def test_collect_episode_status():
    async def _test():
        backend = AsyncMock()

        backend.fetch_episode_processing.side_effect = [
            {"processing_state": "ENRICHING", "processing_stage": "extracting", "uuid": "e1"},
            {"processing_state": "ENRICHING", "processing_stage": "extracting", "uuid": "e1"},
            {"processing_state": "ENRICHING", "processing_stage": "merging", "uuid": "e1"},
            {"processing_state": "READY", "processing_stage": "ready", "uuid": "e1"},
        ]
        backend.get_queue_depth.return_value = 1

        current, history, timed_out = await _collect_episode_status(
            backend, "e1", timeout_s=1.0, poll_interval_s=0.01
        )

        assert current["processing_state"] == "READY"
        assert len(history) == 3
        assert history[0]["stage"] == "extracting"
        assert history[1]["stage"] == "merging"
        assert history[2]["state"] == "READY"
        assert timed_out is False

    asyncio.run(_test())


def test_collect_episode_status_timeout():
    async def _test():
        backend = AsyncMock()
        backend.fetch_episode_processing.return_value = {
            "processing_state": "ENRICHING", "uuid": "e1"
        }
        backend.get_queue_depth.return_value = 1

        current, history, timed_out = await _collect_episode_status(
            backend, "e1", timeout_s=0.05, poll_interval_s=0.1
        )

        assert timed_out is True
        assert len(history) >= 1

    asyncio.run(_test())
