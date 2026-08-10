"""EventHistory event-consolidation CURSOR query shape + adapter delegation (offline).

No live Neo4j: a recording fake captures the Cypher + params so we can prove the independent
:EventConsolidationWatermark cursor contract is expressed correctly — label separation from the
scalar/counter watermarks, version-aware dirty detection and reset, truncation-safe (recorded_at,
turn_id) paging, cursor advance, and that the adapter delegates ALWAYS read :TurnEvidence with no
Episodic fallback and no global evidence_exists switch.
"""

from __future__ import annotations

import pytest

from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
from menhir.infrastructure.turn_evidence_repository import TurnEvidenceRepository


class _RecNeo4j:
    def __init__(self, rows=None):
        self._rows = rows or []
        self.calls: list[tuple[str, dict]] = []

    def execute(self, query, params=None):
        self.calls.append((query, params or {}))
        return self._rows


@pytest.mark.unit
def test_list_event_dirty_is_cursor_and_version_aware_with_independent_label():
    fake = _RecNeo4j(rows=[{"namespace": "project-a"}])
    out = TurnEvidenceRepository(fake).list_event_dirty_evidence_namespaces(
        perceiver_version="v2", limit=50)
    assert out == ["project-a"]
    q, params = fake.calls[0]
    assert params["pv"] == "v2" and params["limit"] == 50
    # dirty when: no watermark, different perceiver_version, or evidence beyond the stored cursor
    assert "w.perceiver_version <> $pv" in q
    assert "ckey > w.cursor_at" in q and "tuuid > w.cursor_uuid" in q
    # SEPARATE label keyed by the namespace string in group_id — not the scalar/counter watermark
    assert "EventConsolidationWatermark {group_id: t.namespace}" in q
    assert "ScalarConsolidationWatermark" not in q
    assert ":ConsolidationWatermark" not in q
    # only role=user, declarant=user, nonempty text participates
    assert "t.role = 'user' AND t.declarant = 'user'" in q
    assert "t.text IS NOT NULL AND t.text <> ''" in q


@pytest.mark.unit
def test_load_next_batch_pages_on_recorded_at_then_turn_id():
    fake = _RecNeo4j(rows=[{
        "uuid": "t0", "valid_at": "2026-07-01T00:00:00Z", "cursor_at": "2026-07-05T00:00:00Z",
        "content": "keep this in mind", "source_kind": "chat", "session_id": "s1",
    }])
    out = TurnEvidenceRepository(fake).load_next_event_evidence_batch(
        "project-a", perceiver_version="v1", limit=100)
    assert out and out[0]["uuid"] == "t0"
    assert out[0]["valid_at"] == "2026-07-01T00:00:00Z"
    assert out[0]["cursor_at"] == "2026-07-05T00:00:00Z"
    assert out[0]["content"] == "keep this in mind"
    assert out[0]["source_kind"] == "chat" and out[0]["session_id"] == "s1"
    q, params = fake.calls[0]
    assert params == {"ns": "project-a", "pv": "v1", "limit": 100}
    assert "EventConsolidationWatermark {group_id: $ns}" in q
    assert "w.perceiver_version <> $pv" in q                # version mismatch resets the cursor
    assert "ckey > cca OR (ckey = cca AND t.turn_id > cu)" in q  # strict (recorded_at, turn_id) paging
    assert "ORDER BY ckey, t.turn_id" in q                  # deterministic truncation-safe order
    # the two times are returned SEPARATELY: valid_at is world-time, cursor_at is the monotonic key
    assert "toString(coalesce(t.occurred_at, t.recorded_at)) AS valid_at" in q
    assert "toString(ckey) AS cursor_at" in q
    assert "t.role = 'user' AND t.declarant = 'user'" in q
    assert "LIMIT $limit" in q


@pytest.mark.unit
def test_advance_cursor_stamps_independent_event_watermark_position_and_version():
    fake = _RecNeo4j()
    TurnEvidenceRepository(fake).advance_event_cursor(
        "project-a", cursor_at="2026-07-03T00:00:00Z", cursor_uuid="t2",
        perceiver_version="v1", at="2026-07-10T00:00:00Z")
    q, params = fake.calls[0]
    assert params["cursor_at"] == "2026-07-03T00:00:00Z" and params["cu"] == "t2"
    assert params["pv"] == "v1"
    assert "EventConsolidationWatermark {group_id: $ns}" in q
    assert "w.cursor_at = datetime($cursor_at)" in q and "w.cursor_uuid = $cu" in q
    assert "w.perceiver_version = $pv" in q
    assert "ScalarConsolidationWatermark" not in q
    assert ":ConsolidationWatermark" not in q


class _AdapterStub:
    """Minimal TurnEvidenceRepository stand-in to prove the adapter ALWAYS delegates to TurnEvidence
    with no Episodic fallback / evidence_exists switch."""

    def __init__(self):
        self.calls = []

    def list_event_dirty_evidence_namespaces(self, *, perceiver_version, limit=200):
        self.calls.append(("list", perceiver_version, limit))
        return ["project-a"]

    def load_next_event_evidence_batch(self, namespace, *, perceiver_version, limit=500):
        self.calls.append(("load", namespace, perceiver_version, limit))
        return []

    def advance_event_cursor(self, namespace, *, cursor_at, cursor_uuid, perceiver_version, at):
        self.calls.append(("advance", namespace, cursor_at, cursor_uuid, perceiver_version, at))


def test_adapter_delegates_always_to_turnevidence_only():
    adapter = MemoryGraphAdapter(_RecNeo4j())
    stub = _AdapterStub()
    adapter._turn_evidence = stub

    assert adapter.list_event_dirty_evidence_namespaces(perceiver_version="v1", limit=7) == ["project-a"]
    assert adapter.load_next_event_evidence_batch("project-a", perceiver_version="v1", limit=9) == []
    adapter.advance_event_cursor("project-a", cursor_at="2026-07-03T00:00:00Z", cursor_uuid="t2",
                                 perceiver_version="v1", at="2026-07-10T00:00:00Z")

    assert stub.calls[0] == ("list", "v1", 7)
    assert stub.calls[1] == ("load", "project-a", "v1", 9)
    assert stub.calls[2] == ("advance", "project-a", "2026-07-03T00:00:00Z", "t2", "v1",
                             "2026-07-10T00:00:00Z")
    # the delegates reach _turn_evidence DIRECTLY — no Episodic fallback, no evidence_exists switch
    assert not hasattr(stub, "evidence_exists")
    assert adapter._turn_evidence is stub
