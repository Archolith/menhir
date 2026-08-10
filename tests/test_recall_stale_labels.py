"""Tests for stale-anchor labeling on recall results.

Covers the pure ``label_stale_anchors`` helper and the
``RecallService.recall()`` integration path.
"""

from __future__ import annotations

import pytest

from menhir.domain.recall import RecallResult, ScoredMemory
from menhir.domain.retrieval_trace_models import RelevanceBreakdown
from menhir.services.context_builder import ContextBuilderService
from menhir.services.stale_labeling import STALE_REASON, label_stale_anchors


# ---------------------------------------------------------------------------
# label_stale_anchors — pure dict-level helper
# ---------------------------------------------------------------------------


class TestLabelStaleAnchorsHelper:
    """Test the pure dict-labeling helper in isolation."""

    def test_stale_match_by_uuid(self):
        items = [{"uuid": "m1", "name": "foo", "score": 0.9}]
        stale = [
            {
                "memory_uuid": "m1",
                "name": "foo",
                "project": "menhir",
                "path": "src/foo.py",
                "dirty_at": "2026-07-08T12:00:00Z",
                "anchored_at": "2026-07-01T00:00:00Z",
                "operation": "edit",
            }
        ]
        label_stale_anchors(items, stale)
        assert items[0]["stale_anchor"] is True
        assert items[0]["stale_reason"] == STALE_REASON
        assert items[0]["dirty_at"] == "2026-07-08T12:00:00Z"
        assert items[0]["anchored_at"] == "2026-07-01T00:00:00Z"
        assert items[0]["path"] == "src/foo.py"

    def test_non_matching_item_gets_stale_false(self):
        items = [{"uuid": "m2", "name": "bar", "score": 0.5}]
        stale = [
            {
                "memory_uuid": "m1",
                "name": "foo",
                "path": "src/foo.py",
                "dirty_at": "2026-07-08T12:00:00Z",
                "anchored_at": "2026-07-01T00:00:00Z",
            }
        ]
        label_stale_anchors(items, stale)
        assert items[0]["stale_anchor"] is False
        assert items[0]["uuid"] == "m2"

    def test_stale_metadata_fields_present(self):
        items = [{"uuid": "m1", "name": "foo"}]
        stale = [
            {
                "memory_uuid": "m1",
                "path": "src/foo.py",
                "dirty_at": "2026-07-08T12:00:00Z",
                "anchored_at": "2026-07-01T00:00:00Z",
            }
        ]
        label_stale_anchors(items, stale)
        d = items[0]
        assert d["stale_anchor"] is True
        assert d["stale_reason"] == STALE_REASON
        assert d["dirty_at"] == "2026-07-08T12:00:00Z"
        assert d["anchored_at"] == "2026-07-01T00:00:00Z"
        assert d["path"] == "src/foo.py"

    def test_multiple_items_labeled_independently(self):
        items = [
            {"uuid": "m1", "name": "foo", "score": 0.9},
            {"uuid": "m2", "name": "bar", "score": 0.5},
            {"uuid": "m3", "name": "baz", "score": 0.3},
        ]
        stale = [
            {
                "memory_uuid": "m1",
                "path": "src/foo.py",
                "dirty_at": "t1",
                "anchored_at": "t0",
            },
            {
                "memory_uuid": "m3",
                "path": "src/baz.py",
                "dirty_at": "t2",
                "anchored_at": "t0",
            },
        ]
        label_stale_anchors(items, stale)
        # m1 — stale
        assert items[0]["stale_anchor"] is True
        assert items[0]["path"] == "src/foo.py"
        # m2 — not stale
        assert items[1]["stale_anchor"] is False
        assert "path" not in items[1] or items[1].get("path") is None
        # m3 — stale
        assert items[2]["stale_anchor"] is True
        assert items[2]["path"] == "src/baz.py"

    def test_no_items_removed(self):
        items = [
            {"uuid": "m1", "name": "foo"},
            {"uuid": "m2", "name": "bar"},
        ]
        stale = [{"memory_uuid": "m1", "path": "src/foo.py"}]
        original_count = len(items)
        label_stale_anchors(items, stale)
        assert len(items) == original_count

    def test_no_score_or_rank_change(self):
        items = [
            {"uuid": "m1", "name": "foo", "score": 0.9, "rank": 1},
            {"uuid": "m2", "name": "bar", "score": 0.5, "rank": 2},
        ]
        stale = [{"memory_uuid": "m1", "path": "src/foo.py"}]
        label_stale_anchors(items, stale)
        assert items[0]["score"] == 0.9
        assert items[0]["rank"] == 1
        assert items[1]["score"] == 0.5
        assert items[1]["rank"] == 2

    def test_empty_stale_anchors_labels_all_false(self):
        items = [
            {"uuid": "m1", "name": "foo"},
            {"uuid": "m2", "name": "bar"},
        ]
        label_stale_anchors(items, [])
        assert items[0]["stale_anchor"] is False
        assert items[1]["stale_anchor"] is False

    def test_empty_items_is_noop(self):
        label_stale_anchors([], [{"memory_uuid": "m1", "path": "src/foo.py"}])
        # should not crash

    def test_match_by_name_fallback(self):
        """Fallback to name matching when uuid keys are absent."""
        items = [{"name": "foo behavior"}]
        stale = [
            {
                "memory_uuid": "m1",
                "name": "foo behavior",
                "path": "src/foo.py",
                "dirty_at": "t1",
                "anchored_at": "t0",
            }
        ]
        label_stale_anchors(items, stale)
        assert items[0]["stale_anchor"] is True
        assert items[0]["path"] == "src/foo.py"

    def test_original_uuid_is_preserved(self):
        items = [{"uuid": "m1", "name": "foo"}]
        stale = [{"memory_uuid": "m1", "path": "src/foo.py"}]
        label_stale_anchors(items, stale)
        assert items[0]["uuid"] == "m1"


# ---------------------------------------------------------------------------
# Integration: RecallService.recall() stale labeling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recall_labels_stale_anchors(
    stub_graphiti_client, stub_memory_graph_adapter, tmp_path, monkeypatch
):
    """Verify that recall output includes stale_anchor metadata when the
    adapter returns stale-anchor rows."""
    from menhir.services.scoring_service import ScoringService
    from menhir.services.recall_service import RecallService

    # Wire the stub adapter to return stale anchors for m1
    stale_rows = [
        {
            "memory_uuid": "m1",
            "name": "foo",
            "project": "menhir",
            "path": "src/foo.py",
            "dirty_at": "2026-07-08T12:00:00Z",
            "anchored_at": "2026-07-01T00:00:00Z",
            "operation": "edit",
        }
    ]
    setattr(stub_memory_graph_adapter, "stale_anchored_memories", lambda **k: stale_rows)

    stub_graphiti_client.search_scored_results = [
        ("m1", "foo", 0.85),
        ("m2", "bar", 0.65),
    ]
    stub_memory_graph_adapter.candidate_metadata = [
        {
            "uuid": "m1",
            "name": "foo",
            "scope": "PERSISTENT",
            "type": "SEMANTIC",
            "content": "foo content",
            "summary": None,
            "last_accessed": "2026-07-01T00:00:00Z",
            "edge_count": 2,
            "freshness": "ACTIVE",
            "user_flagged": False,
        },
        {
            "uuid": "m2",
            "name": "bar",
            "scope": "PERSISTENT",
            "type": "SEMANTIC",
            "content": "bar content",
            "summary": None,
            "last_accessed": "2026-07-01T00:00:00Z",
            "edge_count": 1,
            "freshness": "ACTIVE",
            "user_flagged": False,
        },
    ]
    svc = RecallService(
        graphiti_client=stub_graphiti_client,
        graph_adapter=stub_memory_graph_adapter,
        scoring_service=ScoringService(),
    )
    result = await svc.recall("test query")
    assert len(result.results) == 2

    # m1 should be stale-labeled
    m1 = next(r for r in result.results if r.uuid == "m1")
    assert m1.stale_anchor_info is not None
    assert m1.stale_anchor_info["stale_anchor"] is True
    assert m1.stale_anchor_info["stale_reason"] == "file_changed_after_anchor"
    assert m1.stale_anchor_info["path"] == "src/foo.py"

    # m2 should be labeled not stale
    m2 = next(r for r in result.results if r.uuid == "m2")
    assert m2.stale_anchor_info is not None
    assert m2.stale_anchor_info["stale_anchor"] is False


@pytest.mark.asyncio
async def test_recall_stale_failure_does_not_break_recall(
    stub_graphiti_client, stub_memory_graph_adapter, tmp_path, monkeypatch
):
    """When stale_anchored_memories raises, recall should return results
    normally without stale labels."""
    from menhir.services.scoring_service import ScoringService
    from menhir.services.recall_service import RecallService

    def _broken(**k):
        raise RuntimeError("db unavailable")

    setattr(stub_memory_graph_adapter, "stale_anchored_memories", _broken)

    stub_graphiti_client.search_scored_results = [
        ("m1", "foo", 0.85),
    ]
    stub_memory_graph_adapter.candidate_metadata = [
        {
            "uuid": "m1",
            "name": "foo",
            "scope": "PERSISTENT",
            "type": "SEMANTIC",
            "content": "foo content",
            "summary": None,
            "last_accessed": "2026-07-01T00:00:00Z",
            "edge_count": 1,
            "freshness": "ACTIVE",
            "user_flagged": False,
        },
    ]
    svc = RecallService(
        graphiti_client=stub_graphiti_client,
        graph_adapter=stub_memory_graph_adapter,
        scoring_service=ScoringService(),
    )
    result = await svc.recall("test query")
    assert len(result.results) == 1
    # No stale labels applied — stale_anchor_info stays None
    assert result.results[0].stale_anchor_info is None


@pytest.mark.asyncio
async def test_recall_stale_label_no_results_removed(
    stub_graphiti_client, stub_memory_graph_adapter, tmp_path, monkeypatch
):
    """Stale labeling must not change the number of returned results."""
    from menhir.services.scoring_service import ScoringService
    from menhir.services.recall_service import RecallService

    setattr(
        stub_memory_graph_adapter,
        "stale_anchored_memories",
        lambda **k: [{"memory_uuid": "m1", "path": "src/foo.py",
                       "dirty_at": "t1", "anchored_at": "t0"}],
    )

    stub_graphiti_client.search_scored_results = [
        ("m1", "foo", 0.85),
        ("m2", "bar", 0.65),
        ("m3", "baz", 0.45),
    ]
    stub_memory_graph_adapter.candidate_metadata = [
        {"uuid": u, "name": n, "scope": "PERSISTENT", "type": "SEMANTIC",
         "content": f"{n} content", "summary": None,
         "last_accessed": "2026-07-01T00:00:00Z", "edge_count": 1,
         "freshness": "ACTIVE", "user_flagged": False}
        for u, n, _ in stub_graphiti_client.search_scored_results
    ]
    svc = RecallService(
        graphiti_client=stub_graphiti_client,
        graph_adapter=stub_memory_graph_adapter,
        scoring_service=ScoringService(),
    )
    result = await svc.recall("test query", limit=10)
    assert len(result.results) == 3


@pytest.mark.asyncio
async def test_recall_stale_label_no_rank_change(
    stub_graphiti_client, stub_memory_graph_adapter, tmp_path, monkeypatch
):
    """Stale labeling must not change scores or ordering."""
    from menhir.services.scoring_service import ScoringService
    from menhir.services.recall_service import RecallService

    setattr(
        stub_memory_graph_adapter,
        "stale_anchored_memories",
        lambda **k: [{"memory_uuid": "m1", "path": "src/foo.py",
                       "dirty_at": "t1", "anchored_at": "t0"}],
    )

    stub_graphiti_client.search_scored_results = [
        ("m1", "foo", 0.85),
        ("m2", "bar", 0.65),
    ]
    stub_memory_graph_adapter.candidate_metadata = [
        {"uuid": u, "name": n, "scope": "PERSISTENT", "type": "SEMANTIC",
         "content": f"{n} content", "summary": None,
         "last_accessed": "2026-07-01T00:00:00Z", "edge_count": 1,
         "freshness": "ACTIVE", "user_flagged": False}
        for u, n, _ in stub_graphiti_client.search_scored_results
    ]
    svc = RecallService(
        graphiti_client=stub_graphiti_client,
        graph_adapter=stub_memory_graph_adapter,
        scoring_service=ScoringService(),
    )
    # Recall without stale flagging first
    result_no_stale = await svc.recall("test query")
    # Recall with stale flagging
    result_with_stale = await svc.recall("test query")

    for no_stale, with_stale in zip(result_no_stale.results, result_with_stale.results):
        assert no_stale.uuid == with_stale.uuid
        assert no_stale.final_score == with_stale.final_score


# ---------------------------------------------------------------------------
# _compact_scored_item stale output
# ---------------------------------------------------------------------------


class TestCompactScoredItemStaleOutput:
    """Verify that the MCP formatter includes stale fields correctly."""

    def test_stale_fields_included_when_present(self):
        from types import SimpleNamespace
        from menhir.mcp.formatters import _compact_scored_item

        scored = SimpleNamespace(
            uuid="m1",
            name="foo",
            scope="PERSISTENT",
            final_score=0.9,
            breakdown=SimpleNamespace(
                semantic_similarity=0.85,
                adjacency_bonus=0.05,
                recency_bonus=0.02,
                prominence_bonus=0.003,
                conflict_bonus=0.0,
                type_boost=0.0,
                preset="knowledge",
                alpha=0.2,
                beta=0.1,
                gamma=0.1,
                delta=0.0,
            ),
            memory_type="SEMANTIC",
            temporal_facts=(),
            stale_anchor_info={
                "stale_anchor": True,
                "stale_reason": "file_changed_after_anchor",
                "dirty_at": "2026-07-08T12:00:00Z",
                "anchored_at": "2026-07-01T00:00:00Z",
                "path": "src/foo.py",
            },
            content="foo content",
            warden_label=None,
            view_kind=None,
            is_superseded_view=False,
            summary=None,
        )
        item = _compact_scored_item(scored, compact=False)
        assert item["stale_anchor"] is True
        assert item["stale_reason"] == "file_changed_after_anchor"
        assert item["dirty_at"] == "2026-07-08T12:00:00Z"
        assert item["anchored_at"] == "2026-07-01T00:00:00Z"
        assert item["path"] == "src/foo.py"

    def test_stale_false_included_when_present(self):
        from types import SimpleNamespace
        from menhir.mcp.formatters import _compact_scored_item

        scored = SimpleNamespace(
            uuid="m2",
            name="bar",
            scope="PERSISTENT",
            final_score=0.5,
            breakdown=SimpleNamespace(
                semantic_similarity=0.45,
                adjacency_bonus=0.02,
                recency_bonus=0.01,
                prominence_bonus=0.001,
                conflict_bonus=0.0,
                type_boost=0.0,
                preset="knowledge",
                alpha=0.2,
                beta=0.1,
                gamma=0.1,
                delta=0.0,
            ),
            memory_type="SEMANTIC",
            temporal_facts=(),
            stale_anchor_info={"stale_anchor": False},
            content="bar content",
            warden_label=None,
            view_kind=None,
            is_superseded_view=False,
            summary=None,
        )
        item = _compact_scored_item(scored, compact=False)
        assert item["stale_anchor"] is False
        # No stale-only fields when not stale
        assert "stale_reason" not in item or item["stale_reason"] is None

    def test_stale_fields_omitted_when_unlabeled(self):
        from types import SimpleNamespace
        from menhir.mcp.formatters import _compact_scored_item

        scored = SimpleNamespace(
            uuid="m3",
            name="baz",
            scope="PERSISTENT",
            final_score=0.3,
            breakdown=SimpleNamespace(
                semantic_similarity=0.3,
                adjacency_bonus=0.0,
                recency_bonus=0.0,
                prominence_bonus=0.0,
                conflict_bonus=0.0,
                type_boost=0.0,
                preset="knowledge",
                alpha=0.2,
                beta=0.1,
                gamma=0.1,
                delta=0.0,
            ),
            memory_type="SEMANTIC",
            temporal_facts=(),
            stale_anchor_info=None,
            content="baz content",
            warden_label=None,
            view_kind=None,
            is_superseded_view=False,
            summary=None,
        )
        item = _compact_scored_item(scored, compact=False)
        assert "stale_anchor" not in item

    def test_stale_fields_included_in_compact_mode(self):
        """Stale metadata is decision-relevant and should appear in compact mode."""
        from types import SimpleNamespace
        from menhir.mcp.formatters import _compact_scored_item

        scored = SimpleNamespace(
            uuid="m1",
            name="foo",
            scope="PERSISTENT",
            final_score=0.9,
            breakdown=SimpleNamespace(
                semantic_similarity=0.85,
                adjacency_bonus=0.05,
                recency_bonus=0.02,
                prominence_bonus=0.003,
                conflict_bonus=0.0,
                type_boost=0.0,
                preset="knowledge",
                alpha=0.2,
                beta=0.1,
                gamma=0.1,
                delta=0.0,
            ),
            memory_type="SEMANTIC",
            temporal_facts=(),
            stale_anchor_info={
                "stale_anchor": True,
                "stale_reason": "file_changed_after_anchor",
                "dirty_at": "2026-07-08T12:00:00Z",
                "anchored_at": "2026-07-01T00:00:00Z",
                "path": "src/foo.py",
            },
            content="foo content",
            warden_label=None,
            view_kind=None,
            is_superseded_view=False,
            summary=None,
        )
        item = _compact_scored_item(scored, compact=True)
        assert item["stale_anchor"] is True
        assert item["stale_reason"] == "file_changed_after_anchor"

    # --- Advisory field tests ---

    def test_stale_item_includes_stale_action(self):
        from types import SimpleNamespace
        from menhir.services.stale_labeling import STALE_ACTION
        from menhir.mcp.formatters import _compact_scored_item

        scored = SimpleNamespace(
            uuid="m1", name="foo", scope="PERSISTENT", final_score=0.9,
            breakdown=SimpleNamespace(
                semantic_similarity=0.85, adjacency_bonus=0.05, recency_bonus=0.02,
                prominence_bonus=0.003, conflict_bonus=0.0, type_boost=0.0,
                preset="knowledge", alpha=0.2, beta=0.1, gamma=0.1, delta=0.0,
            ),
            memory_type="SEMANTIC", temporal_facts=(),
            stale_anchor_info={"stale_anchor": True, "stale_reason": "file_changed_after_anchor",
                               "dirty_at": "t1", "anchored_at": "t0", "path": "src/foo.py"},
            content="foo", warden_label=None, view_kind=None,
            is_superseded_view=False, summary=None,
        )
        item = _compact_scored_item(scored, compact=False)
        assert item["stale_action"] == STALE_ACTION

    def test_stale_item_includes_stale_advisory(self):
        from types import SimpleNamespace
        from menhir.services.stale_labeling import STALE_ADVISORY
        from menhir.mcp.formatters import _compact_scored_item

        scored = SimpleNamespace(
            uuid="m1", name="foo", scope="PERSISTENT", final_score=0.9,
            breakdown=SimpleNamespace(
                semantic_similarity=0.85, adjacency_bonus=0.05, recency_bonus=0.02,
                prominence_bonus=0.003, conflict_bonus=0.0, type_boost=0.0,
                preset="knowledge", alpha=0.2, beta=0.1, gamma=0.1, delta=0.0,
            ),
            memory_type="SEMANTIC", temporal_facts=(),
            stale_anchor_info={"stale_anchor": True, "stale_reason": "file_changed_after_anchor",
                               "dirty_at": "t1", "anchored_at": "t0", "path": "src/foo.py"},
            content="foo", warden_label=None, view_kind=None,
            is_superseded_view=False, summary=None,
        )
        item = _compact_scored_item(scored, compact=False)
        assert item["stale_advisory"] == STALE_ADVISORY

    def test_stale_advisory_tells_agent_to_inspect_current_file(self):
        from types import SimpleNamespace
        from menhir.mcp.formatters import _compact_scored_item

        scored = SimpleNamespace(
            uuid="m1", name="foo", scope="PERSISTENT", final_score=0.9,
            breakdown=SimpleNamespace(
                semantic_similarity=0.85, adjacency_bonus=0.05, recency_bonus=0.02,
                prominence_bonus=0.003, conflict_bonus=0.0, type_boost=0.0,
                preset="knowledge", alpha=0.2, beta=0.1, gamma=0.1, delta=0.0,
            ),
            memory_type="SEMANTIC", temporal_facts=(),
            stale_anchor_info={"stale_anchor": True, "stale_reason": "file_changed_after_anchor",
                               "dirty_at": "t1", "anchored_at": "t0", "path": "src/foo.py"},
            content="foo", warden_label=None, view_kind=None,
            is_superseded_view=False, summary=None,
        )
        item = _compact_scored_item(scored, compact=False)
        adv = item["stale_advisory"]
        assert "Inspect the current file" in adv
        assert "before relying" in adv

    def test_stale_false_item_does_not_include_advisory_fields(self):
        from types import SimpleNamespace
        from menhir.mcp.formatters import _compact_scored_item

        scored = SimpleNamespace(
            uuid="m2", name="bar", scope="PERSISTENT", final_score=0.5,
            breakdown=SimpleNamespace(
                semantic_similarity=0.45, adjacency_bonus=0.02, recency_bonus=0.01,
                prominence_bonus=0.001, conflict_bonus=0.0, type_boost=0.0,
                preset="knowledge", alpha=0.2, beta=0.1, gamma=0.1, delta=0.0,
            ),
            memory_type="SEMANTIC", temporal_facts=(),
            stale_anchor_info={"stale_anchor": False},
            content="bar", warden_label=None, view_kind=None,
            is_superseded_view=False, summary=None,
        )
        item = _compact_scored_item(scored, compact=False)
        assert item["stale_anchor"] is False
        assert "stale_action" not in item
        assert "stale_advisory" not in item

    def test_unlabeled_item_does_not_include_advisory_fields(self):
        from types import SimpleNamespace
        from menhir.mcp.formatters import _compact_scored_item

        scored = SimpleNamespace(
            uuid="m3", name="baz", scope="PERSISTENT", final_score=0.3,
            breakdown=SimpleNamespace(
                semantic_similarity=0.3, adjacency_bonus=0.0, recency_bonus=0.0,
                prominence_bonus=0.0, conflict_bonus=0.0, type_boost=0.0,
                preset="knowledge", alpha=0.2, beta=0.1, gamma=0.1, delta=0.0,
            ),
            memory_type="SEMANTIC", temporal_facts=(),
            stale_anchor_info=None,
            content="baz", warden_label=None, view_kind=None,
            is_superseded_view=False, summary=None,
        )
        item = _compact_scored_item(scored, compact=False)
        assert "stale_anchor" not in item
        assert "stale_action" not in item
        assert "stale_advisory" not in item

    def test_advisory_fields_in_compact_mode(self):
        from types import SimpleNamespace
        from menhir.services.stale_labeling import STALE_ACTION, STALE_ADVISORY
        from menhir.mcp.formatters import _compact_scored_item

        scored = SimpleNamespace(
            uuid="m1", name="foo", scope="PERSISTENT", final_score=0.9,
            breakdown=SimpleNamespace(
                semantic_similarity=0.85, adjacency_bonus=0.05, recency_bonus=0.02,
                prominence_bonus=0.003, conflict_bonus=0.0, type_boost=0.0,
                preset="knowledge", alpha=0.2, beta=0.1, gamma=0.1, delta=0.0,
            ),
            memory_type="SEMANTIC", temporal_facts=(),
            stale_anchor_info={"stale_anchor": True, "stale_reason": "file_changed_after_anchor",
                               "dirty_at": "t1", "anchored_at": "t0", "path": "src/foo.py"},
            content="foo", warden_label=None, view_kind=None,
            is_superseded_view=False, summary=None,
        )
        item = _compact_scored_item(scored, compact=True)
        assert item["stale_action"] == STALE_ACTION
        assert item["stale_advisory"] == STALE_ADVISORY

    def test_advisory_fields_in_full_mode(self):
        from types import SimpleNamespace
        from menhir.services.stale_labeling import STALE_ACTION, STALE_ADVISORY
        from menhir.mcp.formatters import _compact_scored_item

        scored = SimpleNamespace(
            uuid="m1", name="foo", scope="PERSISTENT", final_score=0.9,
            breakdown=SimpleNamespace(
                semantic_similarity=0.85, adjacency_bonus=0.05, recency_bonus=0.02,
                prominence_bonus=0.003, conflict_bonus=0.0, type_boost=0.0,
                preset="knowledge", alpha=0.2, beta=0.1, gamma=0.1, delta=0.0,
            ),
            memory_type="SEMANTIC", temporal_facts=(),
            stale_anchor_info={"stale_anchor": True, "stale_reason": "file_changed_after_anchor",
                               "dirty_at": "t1", "anchored_at": "t0", "path": "src/foo.py"},
            content="foo", warden_label=None, view_kind=None,
            is_superseded_view=False, summary=None,
        )
        item = _compact_scored_item(scored, compact=False)
        assert item["stale_action"] == STALE_ACTION
        assert item["stale_advisory"] == STALE_ADVISORY


# ---------------------------------------------------------------------------
# _compact_memory_item stale handling
# ---------------------------------------------------------------------------


class TestCompactMemoryItemStaleOutput:
    """Verify _compact_memory_item passes through stale fields."""

    def test_stale_memory_item_includes_advisory(self):
        from menhir.services.stale_labeling import STALE_ACTION, STALE_ADVISORY
        from menhir.mcp.formatters import _compact_memory_item

        row = {
            "uuid": "m1",
            "name": "foo",
            "scope": "PERSISTENT",
            "type": "SEMANTIC",
            "summary": "foo content",
            "stale_anchor_info": {
                "stale_anchor": True,
                "stale_reason": "file_changed_after_anchor",
                "dirty_at": "2026-07-08T12:00:00Z",
                "anchored_at": "2026-07-01T00:00:00Z",
                "path": "src/foo.py",
            },
        }
        item = _compact_memory_item(row, tag="relevant")
        assert item["stale_anchor"] is True
        assert item["stale_action"] == STALE_ACTION
        assert item["stale_advisory"] == STALE_ADVISORY

    def test_non_stale_memory_item_omits_advisory(self):
        from menhir.mcp.formatters import _compact_memory_item

        row = {
            "uuid": "m2",
            "name": "bar",
            "scope": "PERSISTENT",
            "type": "SEMANTIC",
            "summary": "bar content",
            "stale_anchor_info": {"stale_anchor": False},
        }
        item = _compact_memory_item(row, tag="relevant")
        assert item["stale_anchor"] is False
        assert "stale_action" not in item
        assert "stale_advisory" not in item

    def test_unlabeled_memory_item_omits_stale_fields(self):
        from menhir.mcp.formatters import _compact_memory_item

        row = {
            "uuid": "m3",
            "name": "baz",
            "scope": "PERSISTENT",
            "type": "SEMANTIC",
            "summary": "baz content",
        }
        item = _compact_memory_item(row, tag="recent")
        assert "stale_anchor" not in item
        assert "stale_action" not in item
        assert "stale_advisory" not in item

    def test_advisory_tells_agent_to_inspect_file(self):
        from menhir.mcp.formatters import _compact_memory_item

        row = {
            "uuid": "m1",
            "name": "foo",
            "scope": "PERSISTENT",
            "type": "SEMANTIC",
            "summary": "foo content",
            "stale_anchor_info": {
                "stale_anchor": True,
                "stale_reason": "file_changed_after_anchor",
                "dirty_at": "t1",
                "anchored_at": "t0",
                "path": "src/foo.py",
            },
        }
        item = _compact_memory_item(row, tag="relevant")
        assert "Inspect the current file" in item["stale_advisory"]
        assert "before relying" in item["stale_advisory"]


# ---------------------------------------------------------------------------
# ContextBuilderService stale advisory output
# ---------------------------------------------------------------------------


class TestContextBuilderStaleAdvisory:
    """Verify ContextBuilderService.build_context() includes stale advisory.

    Uses the same AsyncMock pattern as test_context_builder.py.
    """

    _DEFAULT_BREAKDOWN = RelevanceBreakdown(
        semantic_similarity=0.9, adjacency_bonus=0.0, recency_bonus=0.0,
        prominence_bonus=0.0, conflict_bonus=0.0, type_boost=0.0,
        preset="knowledge", alpha=0.2, beta=0.1, gamma=0.1, delta=0.0,
    )

    def _mem(self, uuid: str, name: str, content: str, score: float,
             stale_info: dict | None = None) -> ScoredMemory:
        return ScoredMemory(
            uuid=uuid, name=name, content=content, scope="PERSISTENT",
            memory_type="SEMANTIC", final_score=score,
            breakdown=self._DEFAULT_BREAKDOWN,
            stale_anchor_info=stale_info,
        )

    def _recall_result(self, memories: list[ScoredMemory]) -> RecallResult:
        return RecallResult(
            query="test query", preset="knowledge", results=memories,
            candidates_evaluated=len(memories), nodes_touched=len(memories),
        )

    def _build_service(self, recall_result: RecallResult) -> ContextBuilderService:
        from unittest.mock import AsyncMock

        mock_recall = AsyncMock()
        mock_recall.recall = AsyncMock(return_value=recall_result)
        return ContextBuilderService(recall_service=mock_recall)

    @pytest.mark.asyncio
    async def test_stale_memory_includes_advisory_in_output(self):
        stale = {
            "stale_anchor": True, "stale_reason": "file_changed_after_anchor",
            "dirty_at": "2026-07-08T12:00:00Z",
            "anchored_at": "2026-07-01T00:00:00Z",
            "path": "src/foo.py",
        }
        mem = self._mem("m1", "foo", "foo content", 0.9, stale_info=stale)
        svc = self._build_service(self._recall_result([mem]))

        result = await svc.build_context("test", max_tokens=5000)
        assert "⚠️ Stale file anchor" in result.context
        assert "src/foo.py" in result.context
        assert "inspect the current file" in result.context

    @pytest.mark.asyncio
    async def test_tight_budget_does_not_emit_stale_without_advisory(self):
        """When budget is too tight for memory + advisory, neither appears."""
        stale = {
            "stale_anchor": True, "stale_reason": "file_changed_after_anchor",
            "dirty_at": "t1", "anchored_at": "t0", "path": "src/foo.py",
        }
        mem = self._mem("m1", "foo", "foo content " * 500, 0.9, stale_info=stale)
        svc = self._build_service(self._recall_result([mem]))

        result = await svc.build_context("test", max_tokens=1)
        # Neither memory nor warning appears
        assert "Stale file anchor" not in result.context
        assert "foo" not in result.context

    @pytest.mark.asyncio
    async def test_non_stale_memory_omits_advisory(self):
        mem = self._mem("m2", "bar", "bar content", 0.5,
                        stale_info={"stale_anchor": False})
        svc = self._build_service(self._recall_result([mem]))

        result = await svc.build_context("test", max_tokens=5000)
        assert "⚠️" not in result.context

    @pytest.mark.asyncio
    async def test_unlabeled_memory_omits_advisory(self):
        mem = self._mem("m3", "baz", "baz content", 0.3, stale_info=None)
        svc = self._build_service(self._recall_result([mem]))

        result = await svc.build_context("test", max_tokens=5000)
        assert "⚠️" not in result.context

    @pytest.mark.asyncio
    async def test_memory_ids_and_count_correct_with_stale(self):
        stale1 = {
            "stale_anchor": True, "stale_reason": "file_changed_after_anchor",
            "dirty_at": "t1", "anchored_at": "t0", "path": "src/a.py",
        }
        stale2 = {
            "stale_anchor": True, "stale_reason": "file_changed_after_anchor",
            "dirty_at": "t2", "anchored_at": "t0", "path": "src/b.py",
        }
        mems = [
            self._mem("m1", "foo", "foo content", 0.9, stale_info=stale1),
            self._mem("m2", "bar", "bar content", 0.5, stale_info=None),
            self._mem("m3", "baz", "baz content", 0.3, stale_info=stale2),
        ]
        svc = self._build_service(self._recall_result(mems))

        result = await svc.build_context("test", max_tokens=5000)
        assert result.memory_count == 3
        assert result.memory_ids == ["m1", "m2", "m3"]
        assert "Stale file anchor" in result.context
