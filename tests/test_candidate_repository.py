"""Unit tests for the CANDIDATE (review-tier) direct-write path.

Covers CandidateRepository plus its MemoryGraphAdapter delegate. No live Neo4j:
a stub captures the Cypher and params and echoes a row back so the create/refresh
(created flag) logic is exercised deterministically.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from menhir.infrastructure.candidate_repository import (
    CandidateRepository,
    _MAX_NOTES,
    _MAX_NOTE_LEN,
    _cap_notes,
)
from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter


@dataclass
class _EchoNeo4j:
    """Stub Neo4j that records calls and echoes a create-shaped row.

    ``created_at`` echoes the ``now`` param so create_candidate computes created=True;
    set ``echo_created_at`` to a different value to simulate an ON MATCH refresh.
    """

    echo_created_at: str | None = None
    calls: list[dict[str, object]] = field(default_factory=list)

    def execute(self, query: str, params: dict[str, object] | None = None) -> list[dict[str, object]]:
        p = params or {}
        self.calls.append({"query": query, "params": p})
        return [
            {
                "uuid": p.get("uuid"),
                "scope": "CANDIDATE",
                "cluster_id": p.get("cluster_id"),
                "evidence_strength": p.get("evidence_strength"),
                "distinct_sessions": p.get("distinct_sessions"),
                "created_at": self.echo_created_at or p.get("now"),
                "last_seen": p.get("last_seen"),
            }
        ]


@dataclass
class _CountNeo4j:
    """Stub Neo4j that returns a single count row (for promote/reject paths)."""

    count_key: str
    count: int
    calls: list[dict[str, object]] = field(default_factory=list)

    def execute(self, query: str, params: dict[str, object] | None = None) -> list[dict[str, object]]:
        self.calls.append({"query": query, "params": params or {}})
        return [{self.count_key: self.count}]


@pytest.mark.unit
def test_cap_notes_truncates_count_and_length() -> None:
    notes = [f"note {i}" for i in range(_MAX_NOTES + 5)] + ["x" * (_MAX_NOTE_LEN + 50)]
    capped = _cap_notes(notes)
    assert len(capped) == _MAX_NOTES
    assert all(len(n) <= _MAX_NOTE_LEN for n in capped)
    # Empty/whitespace notes are dropped.
    assert _cap_notes(["  ", "", "keep"]) == ["keep"]
    assert _cap_notes(None) == []


@pytest.mark.unit
def test_create_candidate_writes_candidate_scope_unflagged_and_idempotency_key() -> None:
    neo4j = _EchoNeo4j()
    repo = CandidateRepository(neo4j)

    result = repo.create_candidate(
        content="Agents keep re-deriving the menhir test command.",
        source="painscan-friction",
        cluster_id="P017",
        label="repeated test-command rediscovery",
        kind="friction",
        candidate_type="tooling",
        evidence_strength="COMMON",
        distinct_sessions=4,
        notes=["sess A note", "sess B note"],
    )

    assert result["created"] is True
    assert result["scope"] == "CANDIDATE"
    assert result["cluster_id"] == "P017"

    call = neo4j.calls[0]
    query, params = call["query"], call["params"]
    # Idempotency key is (source, cluster_id) via MERGE.
    assert "MERGE (n:Entity {source: $source, candidate_cluster_id: $cluster_id})" in query
    assert params["source"] == "painscan-friction"
    assert params["cluster_id"] == "P017"
    # Review-tier invariants: CANDIDATE scope, never user-flagged (flagged would hit
    # the lifecycle auto-promote shortcut that skips the contradiction check).
    assert "n.scope = 'CANDIDATE'" in query
    assert "n.user_flagged = false" in query
    # ON MATCH refreshes provenance only (never scope/content).
    assert "ON MATCH SET" in query
    on_match_block = query.split("ON MATCH SET", 1)[1].split("RETURN", 1)[0]
    assert "n.scope" not in on_match_block
    assert "n.content" not in on_match_block


@pytest.mark.unit
def test_create_candidate_reemit_reports_not_created() -> None:
    neo4j = _EchoNeo4j(echo_created_at="2020-01-01T00:00:00+00:00")
    repo = CandidateRepository(neo4j)

    result = repo.create_candidate(
        content="same cluster again",
        source="painscan-friction",
        cluster_id="P017",
        label="repeated",
        evidence_strength="COMMON",
        distinct_sessions=5,
    )
    assert result["created"] is False


@pytest.mark.unit
def test_list_and_fetch_candidates_filter_on_candidate_scope() -> None:
    neo4j = _EchoNeo4j()
    repo = CandidateRepository(neo4j)

    repo.list_candidates(source="painscan-friction", limit=10)
    list_call = neo4j.calls[0]
    assert "n.scope = 'CANDIDATE'" in list_call["query"]
    assert "($source IS NULL OR n.source = $source)" in list_call["query"]
    assert list_call["params"]["limit"] == 10

    neo4j.calls.clear()
    repo.fetch_candidate("cand-uuid-1")
    fetch_call = neo4j.calls[0]
    assert "n.scope = 'CANDIDATE'" in fetch_call["query"]
    assert fetch_call["params"]["uuid"] == "cand-uuid-1"


@pytest.mark.unit
def test_promote_candidate_guards_on_candidate_scope() -> None:
    neo4j = _EchoNeo4j()
    # Echo returns a create-shaped row; override execute for count semantics.
    neo4j_count = _CountNeo4j(count_key="promoted", count=1)
    repo = CandidateRepository(neo4j_count)

    assert repo.promote_candidate("cand-1") is True
    query = neo4j_count.calls[0]["query"]
    assert "n.scope = 'CANDIDATE'" in query
    assert "n.scope = 'PERSISTENT'" in query

    neo4j_zero = _CountNeo4j(count_key="promoted", count=0)
    assert CandidateRepository(neo4j_zero).promote_candidate("not-a-candidate") is False


@pytest.mark.unit
def test_reject_candidate_detach_deletes_only_candidates() -> None:
    neo4j = _CountNeo4j(count_key="rejected", count=1)
    repo = CandidateRepository(neo4j)

    assert repo.reject_candidate("cand-1") is True
    query = neo4j.calls[0]["query"]
    assert "n.scope = 'CANDIDATE'" in query
    assert "DETACH DELETE n" in query

    neo4j_zero = _CountNeo4j(count_key="rejected", count=0)
    assert CandidateRepository(neo4j_zero).reject_candidate("missing") is False


@pytest.mark.unit
def test_adapter_delegates_candidate_methods() -> None:
    neo4j = _EchoNeo4j()
    adapter = MemoryGraphAdapter(neo4j=neo4j)

    out = adapter.create_candidate(
        content="c", source="painscan-friction", cluster_id="P001", label="lbl",
    )
    assert out["scope"] == "CANDIDATE"
    assert any("MERGE (n:Entity" in c["query"] for c in neo4j.calls)
