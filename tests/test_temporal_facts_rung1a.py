"""Unit tests for temporal facts grouping and capping logic (Rung 1A)."""

from __future__ import annotations

import pytest
from menhir.domain.recall import TemporalFact
from menhir.services.recall_service import _build_temporal_facts


class TestBuildTemporalFacts:
    """Test the pure logic of grouping, capping, and role assignment."""

    def test_empty_rows(self) -> None:
        """Empty input yields empty output."""
        result = _build_temporal_facts([])
        assert result == {}

    def test_single_node_single_fact_current(self) -> None:
        """Single current belief (expired_at=None)."""
        rows = [
            {
                "node_uuid": "uuid-1",
                "fact": "is_active",
                "valid_at": "2025-01-01T00:00:00Z",
                "invalid_at": None,
                "created_at": "2025-01-15T10:00:00Z",
                "expired_at": None,
            }
        ]
        result = _build_temporal_facts(rows)
        assert "uuid-1" in result
        facts = result["uuid-1"]
        assert len(facts) == 1
        fact = facts[0]
        assert fact.fact == "is_active"
        assert fact.is_current_belief is True
        assert fact.temporal_role == "current_belief"
        assert fact.created_at == "2025-01-15T10:00:00Z"
        assert fact.expired_at is None

    def test_single_node_single_fact_superseded(self) -> None:
        """Single superseded belief (expired_at set)."""
        rows = [
            {
                "node_uuid": "uuid-1",
                "fact": "was_active",
                "valid_at": "2025-01-01T00:00:00Z",
                "invalid_at": "2025-01-10T00:00:00Z",
                "created_at": "2025-01-05T10:00:00Z",
                "expired_at": "2025-01-20T10:00:00Z",
            }
        ]
        result = _build_temporal_facts(rows)
        assert "uuid-1" in result
        facts = result["uuid-1"]
        assert len(facts) == 1
        fact = facts[0]
        assert fact.is_current_belief is False
        assert fact.temporal_role == "superseded_belief"
        assert fact.expired_at == "2025-01-20T10:00:00Z"

    def test_multiple_nodes(self) -> None:
        """Multiple nodes get separate fact lists."""
        rows = [
            {
                "node_uuid": "uuid-1",
                "fact": "fact-a",
                "valid_at": "2025-01-01T00:00:00Z",
                "invalid_at": None,
                "created_at": "2025-01-15T10:00:00Z",
                "expired_at": None,
            },
            {
                "node_uuid": "uuid-2",
                "fact": "fact-b",
                "valid_at": "2025-01-02T00:00:00Z",
                "invalid_at": None,
                "created_at": "2025-01-16T10:00:00Z",
                "expired_at": None,
            },
        ]
        result = _build_temporal_facts(rows)
        assert len(result) == 2
        assert len(result["uuid-1"]) == 1
        assert result["uuid-1"][0].fact == "fact-a"
        assert len(result["uuid-2"]) == 1
        assert result["uuid-2"][0].fact == "fact-b"

    def test_sorting_by_created_at_desc(self) -> None:
        """Facts sorted by created_at DESC (most recent first)."""
        rows = [
            {
                "node_uuid": "uuid-1",
                "fact": "oldest",
                "valid_at": "2025-01-01T00:00:00Z",
                "invalid_at": None,
                "created_at": "2025-01-10T10:00:00Z",
                "expired_at": None,
            },
            {
                "node_uuid": "uuid-1",
                "fact": "newest",
                "valid_at": "2025-01-05T00:00:00Z",
                "invalid_at": None,
                "created_at": "2025-01-20T10:00:00Z",
                "expired_at": None,
            },
            {
                "node_uuid": "uuid-1",
                "fact": "middle",
                "valid_at": "2025-01-03T00:00:00Z",
                "invalid_at": None,
                "created_at": "2025-01-15T10:00:00Z",
                "expired_at": None,
            },
        ]
        result = _build_temporal_facts(rows)
        facts = result["uuid-1"]
        assert len(facts) == 3
        # Most recent (newest) should be first
        assert facts[0].fact == "newest"
        assert facts[1].fact == "middle"
        assert facts[2].fact == "oldest"

    def test_cap_to_five_most_recent(self) -> None:
        """More than 5 facts are capped to 5 most recent."""
        rows = [
            {
                "node_uuid": "uuid-1",
                "fact": f"fact-{i}",
                "valid_at": f"2025-01-{i+1:02d}T00:00:00Z",
                "invalid_at": None,
                "created_at": f"2025-01-{20+i:02d}T10:00:00Z",
                "expired_at": None,
            }
            for i in range(8)  # Create 8 facts
        ]
        result = _build_temporal_facts(rows)
        facts = result["uuid-1"]
        assert len(facts) == 5
        # Should keep the 5 most recent (fact-7, fact-6, fact-5, fact-4, fact-3)
        assert facts[0].fact == "fact-7"
        assert facts[1].fact == "fact-6"
        assert facts[4].fact == "fact-3"

    def test_none_created_at_sorts_last(self) -> None:
        """Facts with created_at=None sort to the end."""
        rows = [
            {
                "node_uuid": "uuid-1",
                "fact": "with-date",
                "valid_at": "2025-01-01T00:00:00Z",
                "invalid_at": None,
                "created_at": "2025-01-15T10:00:00Z",
                "expired_at": None,
            },
            {
                "node_uuid": "uuid-1",
                "fact": "without-date",
                "valid_at": "2025-01-01T00:00:00Z",
                "invalid_at": None,
                "created_at": None,
                "expired_at": None,
            },
        ]
        result = _build_temporal_facts(rows)
        facts = result["uuid-1"]
        assert len(facts) == 2
        # with-date should come first
        assert facts[0].fact == "with-date"
        assert facts[1].fact == "without-date"

    def test_mixed_current_and_superseded(self) -> None:
        """Mix of current and superseded beliefs, sorted correctly."""
        rows = [
            {
                "node_uuid": "uuid-1",
                "fact": "current-1",
                "valid_at": "2025-01-05T00:00:00Z",
                "invalid_at": None,
                "created_at": "2025-01-20T10:00:00Z",
                "expired_at": None,
            },
            {
                "node_uuid": "uuid-1",
                "fact": "superseded-1",
                "valid_at": "2025-01-01T00:00:00Z",
                "invalid_at": "2025-01-04T00:00:00Z",
                "created_at": "2025-01-10T10:00:00Z",
                "expired_at": "2025-01-19T10:00:00Z",
            },
            {
                "node_uuid": "uuid-1",
                "fact": "current-2",
                "valid_at": "2025-01-10T00:00:00Z",
                "invalid_at": None,
                "created_at": "2025-01-25T10:00:00Z",
                "expired_at": None,
            },
        ]
        result = _build_temporal_facts(rows)
        facts = result["uuid-1"]
        assert len(facts) == 3
        # Most recent is current-2
        assert facts[0].fact == "current-2"
        assert facts[0].is_current_belief is True
        # Then current-1
        assert facts[1].fact == "current-1"
        assert facts[1].is_current_belief is True
        # Then superseded-1
        assert facts[2].fact == "superseded-1"
        assert facts[2].is_current_belief is False

    def test_skip_rows_with_missing_node_uuid(self) -> None:
        """Rows with missing/empty node_uuid are skipped."""
        rows = [
            {
                "node_uuid": "uuid-1",
                "fact": "fact-a",
                "valid_at": "2025-01-01T00:00:00Z",
                "invalid_at": None,
                "created_at": "2025-01-15T10:00:00Z",
                "expired_at": None,
            },
            {
                "node_uuid": None,
                "fact": "orphan-fact",
                "valid_at": "2025-01-02T00:00:00Z",
                "invalid_at": None,
                "created_at": "2025-01-16T10:00:00Z",
                "expired_at": None,
            },
            {
                "node_uuid": "",
                "fact": "empty-uuid-fact",
                "valid_at": "2025-01-03T00:00:00Z",
                "invalid_at": None,
                "created_at": "2025-01-17T10:00:00Z",
                "expired_at": None,
            },
        ]
        result = _build_temporal_facts(rows)
        # Only uuid-1 should be in result
        assert len(result) == 1
        assert "uuid-1" in result
        assert len(result["uuid-1"]) == 1

    def test_temporal_role_assignment(self) -> None:
        """Temporal role is correctly assigned based on expired_at."""
        rows = [
            {
                "node_uuid": "uuid-1",
                "fact": "current",
                "valid_at": "2025-01-01T00:00:00Z",
                "invalid_at": None,
                "created_at": "2025-01-15T10:00:00Z",
                "expired_at": None,
            },
            {
                "node_uuid": "uuid-1",
                "fact": "superseded",
                "valid_at": "2025-01-01T00:00:00Z",
                "invalid_at": "2025-01-10T00:00:00Z",
                "created_at": "2025-01-10T10:00:00Z",
                "expired_at": "2025-01-20T10:00:00Z",
            },
        ]
        result = _build_temporal_facts(rows)
        facts = result["uuid-1"]
        # Most recent (current) is first
        assert facts[0].temporal_role == "current_belief"
        # Superseded is second
        assert facts[1].temporal_role == "superseded_belief"


class TestTemporalFactDataclass:
    """Test TemporalFact dataclass structure."""

    def test_creation_with_all_fields(self) -> None:
        """TemporalFact can be created with all fields."""
        tf = TemporalFact(
            fact="is_active",
            valid_at="2025-01-01T00:00:00Z",
            invalid_at=None,
            created_at="2025-01-15T10:00:00Z",
            expired_at=None,
            is_current_belief=True,
            temporal_role="current_belief",
        )
        assert tf.fact == "is_active"
        assert tf.is_current_belief is True
        assert tf.temporal_role == "current_belief"

    def test_frozen_immutable(self) -> None:
        """TemporalFact is frozen and immutable."""
        tf = TemporalFact(
            fact="test",
            valid_at=None,
            invalid_at=None,
            created_at=None,
            expired_at=None,
            is_current_belief=False,
            temporal_role="superseded_belief",
        )
        with pytest.raises(AttributeError):
            tf.fact = "modified"  # type: ignore
