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
