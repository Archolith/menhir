"""ScalarStateView C.4.3 scalar-consolidation CURSOR query shape (offline).

No live Neo4j: a recording fake captures the Cypher + params so we can prove the cursor/version
contract is expressed correctly (existence-by-cursor dirty check, version-aware paging, cursor
advance). True row-ordering behavior against real data is the C.4.4 live gate; the resumable
semantics are exercised behaviorally through the scheduler fake in test_consolidate_personal_memory.
"""

from __future__ import annotations

import pytest

from menhir.infrastructure.personal_memory_queries import PersonalMemoryRepository


class _RecNeo4j:
    def __init__(self, rows=None):
        self._rows = rows or []
        self.calls: list[tuple[str, dict]] = []

    def execute(self, query, params=None):
        self.calls.append((query, params or {}))
        return self._rows


@pytest.mark.unit
def test_list_scalar_dirty_is_cursor_and_version_aware_on_ingestion_key():
    fake = _RecNeo4j(rows=[{"namespace": "lme-a"}])
    out = PersonalMemoryRepository(fake).list_scalar_dirty_namespaces(perceiver_version="v2", limit=50)
    assert out == ["lme-a"]
    q, params = fake.calls[0]
    assert params["pv"] == "v2" and params["limit"] == 50
    # dirty when: no watermark, different perceiver_version, or an episode beyond the stored cursor;
    # the work-discovery key is INGESTION order coalesce(created_at, valid_at), NOT world-time.
    assert "coalesce(e.created_at, e.valid_at) AS ckey" in q
    assert "w.perceiver_version <> $pv" in q
    assert "ckey > w.cursor_at" in q and "euuid > w.cursor_uuid" in q
    assert "ScalarConsolidationWatermark" in q


@pytest.mark.unit
def test_load_next_batch_pages_on_ingestion_key_and_returns_genuine_valid_at():
    fake = _RecNeo4j(rows=[{"uuid": "e0", "valid_at": "2026-07-01T00:00:00Z",
                            "cursor_at": "2026-07-05T00:00:00Z", "content": "user: x"}])
    out = PersonalMemoryRepository(fake).load_next_scalar_batch(
        "lme-a", perceiver_version="v1", limit=100)
    assert out and out[0]["uuid"] == "e0"
    q, params = fake.calls[0]
    assert params == {"ns": "lme-a", "pv": "v1", "limit": 100}
    assert "w.perceiver_version <> $pv" in q                    # version mismatch resets the cursor
    assert "coalesce(e.created_at, e.valid_at) AS ckey" in q    # cursor keyed on ingestion order
    assert "ckey > cca OR (ckey = cca AND e.uuid > cu)" in q    # strict (cursor_at, uuid) pagination
    assert "ORDER BY ckey, e.uuid" in q                         # deterministic cursor order
    # the two times are returned SEPARATELY so world-time is never conflated with the cursor key
    assert "toString(e.valid_at) AS valid_at" in q
    assert "toString(ckey) AS cursor_at" in q
    assert "LIMIT $limit" in q


@pytest.mark.unit
def test_advance_cursor_stamps_ingestion_position_and_version():
    fake = _RecNeo4j()
    PersonalMemoryRepository(fake).advance_scalar_cursor(
        "lme-a", cursor_at="2026-07-03T00:00:00Z", cursor_uuid="e2",
        perceiver_version="v1", at="2026-07-10T00:00:00Z")
    q, params = fake.calls[0]
    assert params["cursor_at"] == "2026-07-03T00:00:00Z" and params["cu"] == "e2"
    assert params["pv"] == "v1"
    assert "w.cursor_at = datetime($cursor_at)" in q and "w.cursor_uuid = $cu" in q
    assert "w.perceiver_version = $pv" in q
