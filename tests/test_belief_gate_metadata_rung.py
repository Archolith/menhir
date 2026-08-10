"""The belief-gate temporal-marker attach step is pure over fetched fact-rows (stub adapter)."""

from __future__ import annotations

from menhir.services.recall_service import _belief_markers_from_facts


def test_superseded_and_current_markers() -> None:
    rows = [
        {"node_uuid": "u1", "expired_at": "2025-01-02"},   # superseded
        {"node_uuid": "u2", "expired_at": None},            # current only
    ]
    markers = _belief_markers_from_facts(rows)
    assert markers["u1"] == {"belief_superseded": True, "belief_has_temporal": True}
    assert markers["u2"] == {"belief_superseded": False, "belief_has_temporal": True}


def test_no_rows_no_markers() -> None:
    assert _belief_markers_from_facts([]) == {}
