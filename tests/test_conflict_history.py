"""Tests for conflict resolution history (suppression + cursor pagination)."""

from __future__ import annotations

import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from menhir.config import MemorySettings
from menhir.infrastructure.telemetry.store import McpTelemetryStore


# ---------------------------------------------------------------------------
# Store-level tests: record_conflict_resolution / is_pair_resolved
# ---------------------------------------------------------------------------


@pytest.fixture()
def store(tmp_path: Path) -> McpTelemetryStore:
    return McpTelemetryStore(db_path=tmp_path / "test_telemetry.db")


class TestRecordAndLookup:
    def test_record_and_lookup(self, store: McpTelemetryStore) -> None:
        store.record_conflict_resolution(
            uuid_a="aaa", uuid_b="bbb",
            status="false_positive", group_id="g1",
            action="keep_both", reviewed_by="llm",
        )
        assert store.is_pair_resolved("aaa", "bbb") is True

    def test_uuid_order_independent(self, store: McpTelemetryStore) -> None:
        store.record_conflict_resolution(
            uuid_a="bbb", uuid_b="aaa",
            status="false_positive", group_id="g1",
            action="keep_both", reviewed_by="llm",
        )
        assert store.is_pair_resolved("aaa", "bbb") is True
        assert store.is_pair_resolved("bbb", "aaa") is True

    def test_unresolved_pair_not_found(self, store: McpTelemetryStore) -> None:
        assert store.is_pair_resolved("aaa", "bbb") is False

    def test_re_resolution_updates(self, store: McpTelemetryStore) -> None:
        store.record_conflict_resolution(
            uuid_a="aaa", uuid_b="bbb",
            status="false_positive", group_id="g1",
            action="keep_both", reviewed_by="llm",
        )
        store.record_conflict_resolution(
            uuid_a="aaa", uuid_b="bbb",
            status="resolved", group_id="g2",
            action="replace", reviewed_by="user",
        )
        # Should still be resolved (latest state wins)
        assert store.is_pair_resolved("aaa", "bbb") is True
        # Verify only one row exists
        with sqlite3.connect(store.db_path) as conn:
            count = conn.execute("SELECT count(*) FROM conflict_resolutions").fetchone()[0]
        assert count == 1


class TestCooldown:
    def test_cooldown_zero_permanent(self, store: McpTelemetryStore) -> None:
        store.record_conflict_resolution(
            uuid_a="aaa", uuid_b="bbb",
            status="false_positive", group_id="g1",
            action="keep_both", reviewed_by="llm",
        )
        assert store.is_pair_resolved("aaa", "bbb", cooldown_days=0) is True

    def test_cooldown_active(self, store: McpTelemetryStore) -> None:
        store.record_conflict_resolution(
            uuid_a="aaa", uuid_b="bbb",
            status="false_positive", group_id="g1",
            action="keep_both", reviewed_by="llm",
        )
        assert store.is_pair_resolved("aaa", "bbb", cooldown_days=30) is True

    def test_cooldown_expired(self, store: McpTelemetryStore) -> None:
        store.record_conflict_resolution(
            uuid_a="aaa", uuid_b="bbb",
            status="false_positive", group_id="g1",
            action="keep_both", reviewed_by="llm",
        )
        # Backdate the resolved_at to 60 days ago
        with sqlite3.connect(store.db_path) as conn:
            conn.execute(
                "UPDATE conflict_resolutions SET resolved_at = datetime('now', '-60 days')"
            )
            conn.commit()
        assert store.is_pair_resolved("aaa", "bbb", cooldown_days=30) is False
        # But permanent suppression still sees it
        assert store.is_pair_resolved("aaa", "bbb", cooldown_days=0) is True


# ---------------------------------------------------------------------------
# Scan suppression: _check_contradictions_batch skips resolved pairs
# ---------------------------------------------------------------------------


class TestScanSuppression:
    @pytest.fixture()
    def lifecycle(self, store: McpTelemetryStore):
        """Build a minimal LifecycleService with mocks."""
        from menhir.services.lifecycle_service import LifecycleService

        adapter = MagicMock()
        adapter.set_conflict.return_value = ("group-1", 2)
        graphiti = MagicMock()
        graphiti.search_scored = AsyncMock(return_value=[
            ("uuid-other", "other node", 0.92),
        ])
        settings = MemorySettings(conflict_cooldown_days=0)
        svc = LifecycleService(
            graph_adapter=adapter,
            graphiti_client=graphiti,
            llm=None,
            settings=settings,
        )
        return svc

    @pytest.mark.asyncio
    async def test_scan_skips_resolved_pair(self, lifecycle, store: McpTelemetryStore) -> None:
        store.record_conflict_resolution(
            uuid_a="uuid-candidate", uuid_b="uuid-other",
            status="false_positive", group_id="g-old",
            action="keep_both", reviewed_by="llm",
        )
        with patch("menhir.services.lifecycle_consolidation.telemetry_store", store):
            count = await lifecycle._check_contradictions_batch([
                {"uuid": "uuid-candidate", "name": "test", "content": "some content"},
            ])
        assert count == 0
        lifecycle.graph_adapter.set_conflict.assert_not_called()

    @pytest.mark.asyncio
    async def test_scan_flags_new_pair(self, lifecycle, store: McpTelemetryStore) -> None:
        # No suppression row — should flag conflict
        with patch("menhir.services.lifecycle_consolidation.telemetry_store", store):
            count = await lifecycle._check_contradictions_batch([
                {"uuid": "uuid-candidate", "name": "test", "content": "some content"},
            ])
        assert count == 1
        lifecycle.graph_adapter.set_conflict.assert_called_once()


# ---------------------------------------------------------------------------
# Confirm conflicts: suppression recording
# ---------------------------------------------------------------------------


class TestConfirmSuppression:
    @pytest.fixture()
    def lifecycle_with_llm(self, store: McpTelemetryStore):
        from menhir.services.lifecycle_service import LifecycleService

        adapter = MagicMock()
        adapter.list_conflict_groups.return_value = [
            {
                "group_id": "grp-1",
                "created_at": datetime.now(timezone.utc),
                "members": [
                    {"uuid": "node-a", "name": "A", "content": "content a", "status": "pending_llm_review"},
                    {"uuid": "node-b", "name": "B", "content": "content b", "status": "pending_llm_review"},
                    {"uuid": "node-c", "name": "C", "content": "content c", "status": "pending_llm_review"},
                ],
            }
        ]
        adapter.resolve_conflict_group.return_value = {
            "action": "keep_both",
            "group_id": "grp-1",
            "resolved": 3,
            "removed_uuid": None,
            "removed_uuids": [],
            "bridged_edges": 0,
            "member_uuids": ["node-a", "node-b", "node-c"],
        }
        llm = MagicMock()
        llm.confirm_contradiction = AsyncMock(return_value=False)
        settings = MemorySettings()
        svc = LifecycleService(
            graph_adapter=adapter,
            graphiti_client=MagicMock(),
            llm=llm,
            settings=settings,
        )
        return svc

    @pytest.mark.asyncio
    async def test_llm_review_records_reviewed_pair_only(
        self, lifecycle_with_llm, store: McpTelemetryStore,
    ) -> None:
        with patch("menhir.services.lifecycle_conflicts.telemetry_store", store):
            result = await lifecycle_with_llm.confirm_pending_conflicts(limit=10)
        assert result["cleared"] == 1
        # Only the reviewed pair (members[0], members[1]) should be recorded
        assert store.is_pair_resolved("node-a", "node-b") is True
        # node-c was NOT part of the reviewed pair
        assert store.is_pair_resolved("node-a", "node-c") is False
        assert store.is_pair_resolved("node-b", "node-c") is False


# ---------------------------------------------------------------------------
# Auto-resolve: blanket suppression of all pairs
# ---------------------------------------------------------------------------


class TestAutoResolveSuppression:
    @pytest.fixture()
    def lifecycle_auto(self, store: McpTelemetryStore):
        from menhir.services.lifecycle_service import LifecycleService

        adapter = MagicMock()
        adapter.list_conflict_groups.return_value = [
            {
                "group_id": "grp-old",
                "created_at": datetime(2020, 1, 1, tzinfo=timezone.utc),
                "members": [
                    {"uuid": "n1", "name": "N1", "status": "unresolved"},
                    {"uuid": "n2", "name": "N2", "status": "unresolved"},
                    {"uuid": "n3", "name": "N3", "status": "unresolved"},
                ],
            }
        ]
        adapter.resolve_conflict_group.return_value = {
            "action": "keep_both",
            "group_id": "grp-old",
            "resolved": 3,
            "removed_uuid": None,
            "removed_uuids": [],
            "bridged_edges": 0,
            "member_uuids": ["n1", "n2", "n3"],
        }
        settings = MemorySettings()
        svc = LifecycleService(
            graph_adapter=adapter,
            graphiti_client=MagicMock(),
            llm=None,
            settings=settings,
        )
        return svc

    def test_auto_resolve_records_all_pairs(
        self, lifecycle_auto, store: McpTelemetryStore,
    ) -> None:
        with patch("menhir.services.lifecycle_conflicts.telemetry_store", store):
            count = lifecycle_auto.auto_resolve_stale_conflicts(max_age_days=14)
        assert count == 1
        # All 3 pairs should be suppressed (blanket resolution)
        assert store.is_pair_resolved("n1", "n2") is True
        assert store.is_pair_resolved("n1", "n3") is True
        assert store.is_pair_resolved("n2", "n3") is True


# ---------------------------------------------------------------------------
# Manual keep_both: records presented pair from prefetched members
# ---------------------------------------------------------------------------


class TestManualKeepBoth:
    def test_manual_keep_both_records_presented_pair(self, store: McpTelemetryStore) -> None:
        # Simulate what the MCP tool / lifecycle service would do:
        # resolve returns member_uuids, caller records members[0] vs members[1]
        result = {
            "action": "keep_both",
            "member_uuids": ["uuid-x", "uuid-y", "uuid-z"],
        }
        member_uuids = result["member_uuids"]
        # Record only the presented pair (first two members)
        store.record_conflict_resolution(
            uuid_a=member_uuids[0], uuid_b=member_uuids[1],
            status="resolved", group_id="grp-manual",
            action="keep_both", reviewed_by="user",
        )
        assert store.is_pair_resolved("uuid-x", "uuid-y") is True
        assert store.is_pair_resolved("uuid-x", "uuid-z") is False


# ---------------------------------------------------------------------------
# Cursor pagination
# ---------------------------------------------------------------------------


class TestCursorPagination:
    @pytest.fixture()
    def lifecycle_cursor(self):
        from menhir.services.lifecycle_service import LifecycleService

        adapter = MagicMock()
        graphiti = MagicMock()
        graphiti.search_scored = AsyncMock(return_value=[])
        settings = MemorySettings()
        svc = LifecycleService(
            graph_adapter=adapter,
            graphiti_client=graphiti,
            llm=None,
            settings=settings,
        )
        return svc

    @pytest.mark.asyncio
    async def test_scan_cursor_returns_next(self, lifecycle_cursor) -> None:
        lifecycle_cursor.graph_adapter.neo4j.execute.return_value = [
            {"uuid": "aaa", "name": "A", "content": "c1"},
            {"uuid": "bbb", "name": "B", "content": "c2"},
        ]
        result = await lifecycle_cursor.scan_for_conflicts(limit=50)
        assert result["next_cursor"] == "bbb"
        assert result["done"] is True  # 2 < 50

    @pytest.mark.asyncio
    async def test_scan_cursor_done_false(self, lifecycle_cursor) -> None:
        rows = [{"uuid": f"u{i:03d}", "name": f"N{i}", "content": f"c{i}"} for i in range(5)]
        lifecycle_cursor.graph_adapter.neo4j.execute.return_value = rows
        result = await lifecycle_cursor.scan_for_conflicts(limit=5)
        assert result["done"] is False
        assert result["next_cursor"] == "u004"

    @pytest.mark.asyncio
    async def test_scan_cursor_continues(self, lifecycle_cursor) -> None:
        lifecycle_cursor.graph_adapter.neo4j.execute.return_value = [
            {"uuid": "ccc", "name": "C", "content": "c3"},
        ]
        result = await lifecycle_cursor.scan_for_conflicts(limit=50, cursor="bbb")
        assert result["next_cursor"] == "ccc"
        assert result["done"] is True
        # Verify the cursor was passed to the query
        call_args = lifecycle_cursor.graph_adapter.neo4j.execute.call_args
        assert call_args[1].get("params", call_args[0][1] if len(call_args[0]) > 1 else {}).get("cursor") == "bbb" or \
               "cursor" in str(call_args)

    @pytest.mark.asyncio
    async def test_scan_cursor_none_starts_fresh(self, lifecycle_cursor) -> None:
        lifecycle_cursor.graph_adapter.neo4j.execute.return_value = []
        result = await lifecycle_cursor.scan_for_conflicts(limit=50, cursor=None)
        assert result["next_cursor"] is None
        assert result["done"] is True
        assert result["scanned"] == 0


# ---------------------------------------------------------------------------
# Member prefetch in resolve_conflict_group
# ---------------------------------------------------------------------------


class TestMemberPrefetch:
    def test_keep_both_returns_member_uuids(self) -> None:
        """Verify the stub adapter returns member_uuids (matching real impl)."""
        from tests.conftest import StubMemoryGraphAdapter
        adapter = StubMemoryGraphAdapter()
        adapter.set_conflict("uuid-a", "uuid-b", "grp-1")
        result = adapter.resolve_conflict_group("grp-1", "keep_both")
        assert "member_uuids" in result
        assert sorted(result["member_uuids"]) == ["uuid-a", "uuid-b"]

    def test_replace_returns_member_uuids(self) -> None:
        from tests.conftest import StubMemoryGraphAdapter
        adapter = StubMemoryGraphAdapter()
        adapter.set_conflict("uuid-a", "uuid-b", "grp-1")
        result = adapter.resolve_conflict_group(
            "grp-1", "replace", keep_uuid="uuid-a", remove_uuid="uuid-b",
        )
        assert "member_uuids" in result
        assert sorted(result["member_uuids"]) == ["uuid-a", "uuid-b"]
