"""Unit tests for belief invalidation filtering (Rung 1B)."""

from __future__ import annotations

from menhir.services.recall_service import _filter_to_current_beliefs


class TestFilterToCurrentBeliefs:
    """Test the pure logic of filtering rows by expired_at."""

    def test_empty_input(self) -> None:
        """Empty input yields empty output."""
        result = _filter_to_current_beliefs([])
        assert result == []

    def test_all_current_beliefs(self) -> None:
        """Input with only current beliefs (expired_at=None) is unchanged."""
        rows = [
            {
                "node_uuid": "uuid-1",
                "fact": "fact-1",
                "valid_at": "2025-01-01T00:00:00Z",
                "invalid_at": None,
                "created_at": "2025-01-15T10:00:00Z",
                "expired_at": None,
            },
            {
                "node_uuid": "uuid-2",
                "fact": "fact-2",
                "valid_at": "2025-01-02T00:00:00Z",
                "invalid_at": None,
                "created_at": "2025-01-16T10:00:00Z",
                "expired_at": None,
            },
        ]
        result = _filter_to_current_beliefs(rows)
        assert result == rows
        assert len(result) == 2

    def test_all_superseded_beliefs(self) -> None:
        """Input with only superseded beliefs (expired_at set) is filtered out."""
        rows = [
            {
                "node_uuid": "uuid-1",
                "fact": "old-fact-1",
                "valid_at": "2025-01-01T00:00:00Z",
                "invalid_at": "2025-01-10T00:00:00Z",
                "created_at": "2025-01-05T10:00:00Z",
                "expired_at": "2025-01-20T10:00:00Z",
            },
            {
                "node_uuid": "uuid-2",
                "fact": "old-fact-2",
                "valid_at": "2025-01-02T00:00:00Z",
                "invalid_at": "2025-01-11T00:00:00Z",
                "created_at": "2025-01-06T10:00:00Z",
                "expired_at": "2025-01-21T10:00:00Z",
            },
        ]
        result = _filter_to_current_beliefs(rows)
        assert result == []

    def test_mixed_current_and_superseded(self) -> None:
        """Input with mix of current and superseded beliefs returns only current ones."""
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
                "node_uuid": "uuid-2",
                "fact": "superseded-1",
                "valid_at": "2025-01-01T00:00:00Z",
                "invalid_at": "2025-01-04T00:00:00Z",
                "created_at": "2025-01-10T10:00:00Z",
                "expired_at": "2025-01-19T10:00:00Z",
            },
            {
                "node_uuid": "uuid-3",
                "fact": "current-2",
                "valid_at": "2025-01-10T00:00:00Z",
                "invalid_at": None,
                "created_at": "2025-01-25T10:00:00Z",
                "expired_at": None,
            },
        ]
        result = _filter_to_current_beliefs(rows)
        assert len(result) == 2
        # Verify the current beliefs are kept (order preserved)
        assert result[0]["fact"] == "current-1"
        assert result[0]["expired_at"] is None
        assert result[1]["fact"] == "current-2"
        assert result[1]["expired_at"] is None

    def test_filters_on_expired_at_not_invalid_at(self) -> None:
        """Filter keys off expired_at (belief invalidation), NOT invalid_at (world validity).

        A row with invalid_at set but expired_at=None (world fact is invalid but
        belief is still current) must survive the filter.
        """
        rows = [
            {
                "node_uuid": "uuid-1",
                "fact": "world-invalid-but-belief-current",
                "valid_at": "2025-01-01T00:00:00Z",
                "invalid_at": "2025-01-10T00:00:00Z",  # World fact is no longer valid
                "created_at": "2025-01-05T10:00:00Z",
                "expired_at": None,  # But belief is still current
            },
            {
                "node_uuid": "uuid-2",
                "fact": "belief-superseded",
                "valid_at": "2025-01-01T00:00:00Z",
                "invalid_at": None,
                "created_at": "2025-01-10T10:00:00Z",
                "expired_at": "2025-01-20T10:00:00Z",  # Belief is superseded
            },
        ]
        result = _filter_to_current_beliefs(rows)
        assert len(result) == 1
        # The row with world-invalid but belief-current should survive
        assert result[0]["fact"] == "world-invalid-but-belief-current"
        assert result[0]["invalid_at"] == "2025-01-10T00:00:00Z"
        assert result[0]["expired_at"] is None

    def test_row_with_missing_expired_at_key(self) -> None:
        """Rows with missing expired_at key default to None (current belief)."""
        rows = [
            {
                "node_uuid": "uuid-1",
                "fact": "fact-1",
                "valid_at": "2025-01-01T00:00:00Z",
                # expired_at key is missing
            },
            {
                "node_uuid": "uuid-2",
                "fact": "fact-2",
                "valid_at": "2025-01-02T00:00:00Z",
                "expired_at": "2025-01-20T10:00:00Z",  # This one is superseded
            },
        ]
        result = _filter_to_current_beliefs(rows)
        assert len(result) == 1
        assert result[0]["fact"] == "fact-1"

    def test_preserves_order(self) -> None:
        """Filter preserves the order of input rows."""
        rows = [
            {
                "node_uuid": "uuid-1",
                "fact": "first-current",
                "expired_at": None,
            },
            {
                "node_uuid": "uuid-2",
                "fact": "first-superseded",
                "expired_at": "2025-01-20T10:00:00Z",
            },
            {
                "node_uuid": "uuid-3",
                "fact": "second-current",
                "expired_at": None,
            },
            {
                "node_uuid": "uuid-4",
                "fact": "second-superseded",
                "expired_at": "2025-01-21T10:00:00Z",
            },
            {
                "node_uuid": "uuid-5",
                "fact": "third-current",
                "expired_at": None,
            },
        ]
        result = _filter_to_current_beliefs(rows)
        assert len(result) == 3
        assert result[0]["fact"] == "first-current"
        assert result[1]["fact"] == "second-current"
        assert result[2]["fact"] == "third-current"

    def test_multiple_current_beliefs_per_node(self) -> None:
        """Multiple current beliefs for the same node are all preserved."""
        rows = [
            {
                "node_uuid": "uuid-1",
                "fact": "fact-1a",
                "expired_at": None,
            },
            {
                "node_uuid": "uuid-1",
                "fact": "fact-1b",
                "expired_at": None,
            },
            {
                "node_uuid": "uuid-1",
                "fact": "fact-1c-superseded",
                "expired_at": "2025-01-20T10:00:00Z",
            },
        ]
        result = _filter_to_current_beliefs(rows)
        assert len(result) == 2
        assert result[0]["fact"] == "fact-1a"
        assert result[1]["fact"] == "fact-1b"
