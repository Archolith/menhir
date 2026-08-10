"""Tests for bitemporal fact metadata (R3 rung B / Chronostratum Rung 1).

Pins the clock model: current-belief = expired_at IS NULL; as-of-world uses valid_at/
invalid_at; what-we-knew-then uses created_at/expired_at; roles distinguish historical /
not-yet-valid / expired-world / not-yet-known."""

from __future__ import annotations

from menhir.domain.temporal import (
    FactTemporal,
    TemporalQuery,
    TemporalRole,
    format_temporal_provenance,
    order_by_world_time,
    is_current_belief,
    is_valid_at,
    matches_query,
    temporal_role,
    was_known_at,
)


def test_current_belief_is_expired_at_null() -> None:
    assert is_current_belief(FactTemporal(created_at="2026-01-01")) is True
    assert is_current_belief(FactTemporal(created_at="2026-01-01", expired_at="2026-02-01")) is False


def test_is_valid_at_world_window() -> None:
    f = FactTemporal(valid_at="2026-01-01", invalid_at="2026-03-01")
    assert is_valid_at(f, "2026-02-01") is True      # inside window
    assert is_valid_at(f, "2025-12-01") is False     # before valid_at
    assert is_valid_at(f, "2026-03-01") is False     # at invalid_at (exclusive)
    # open-ended still-true fact
    assert is_valid_at(FactTemporal(valid_at="2026-01-01"), "2030-01-01") is True


def test_was_known_at_belief_window() -> None:
    f = FactTemporal(created_at="2026-01-01", expired_at="2026-03-01")
    assert was_known_at(f, "2026-02-01") is True
    assert was_known_at(f, "2025-12-01") is False    # not yet learned
    assert was_known_at(f, "2026-03-01") is False    # belief already expired


def test_matches_query_current_belief() -> None:
    current = FactTemporal(created_at="2026-01-01")
    expired = FactTemporal(created_at="2026-01-01", expired_at="2026-02-01")
    assert matches_query(current, TemporalQuery.CURRENT_BELIEF) is True
    assert matches_query(expired, TemporalQuery.CURRENT_BELIEF) is False


def test_matches_query_as_of_falls_back_to_current_without_pivot() -> None:
    expired = FactTemporal(created_at="2026-01-01", expired_at="2026-02-01")
    # no as_of -> safe default: current-belief filter (never leak history)
    assert matches_query(expired, TemporalQuery.AS_OF_WORLD) is False


def test_matches_query_as_of_world_and_as_known() -> None:
    f = FactTemporal(valid_at="2026-01-01", invalid_at="2026-03-01",
                     created_at="2026-01-05", expired_at="2026-04-01")
    assert matches_query(f, TemporalQuery.AS_OF_WORLD, as_of="2026-02-01") is True
    assert matches_query(f, TemporalQuery.AS_OF_WORLD, as_of="2026-03-15") is False  # world-expired
    assert matches_query(f, TemporalQuery.AS_KNOWN_AT, as_of="2026-02-01") is True
    assert matches_query(f, TemporalQuery.AS_KNOWN_AT, as_of="2026-01-01") is False  # not yet known


def test_temporal_role_historical_dominates() -> None:
    f = FactTemporal(valid_at="2026-01-01", created_at="2026-01-01", expired_at="2026-02-01")
    assert temporal_role(f) is TemporalRole.HISTORICAL


def test_temporal_role_not_yet_known_anachronism() -> None:
    f = FactTemporal(valid_at="2026-01-01", created_at="2026-05-01")
    # as-of before the fact was learned -> temporal leakage guard
    assert temporal_role(f, as_of="2026-02-01") is TemporalRole.NOT_YET_KNOWN


def test_temporal_role_world_edges_and_current() -> None:
    not_yet = FactTemporal(valid_at="2026-06-01", created_at="2026-01-01")
    assert temporal_role(not_yet, as_of="2026-02-01") is TemporalRole.NOT_YET_VALID
    expired_world = FactTemporal(valid_at="2026-01-01", invalid_at="2026-03-01", created_at="2026-01-01")
    assert temporal_role(expired_world, as_of="2026-04-01") is TemporalRole.EXPIRED_WORLD
    assert temporal_role(FactTemporal(valid_at="2026-01-01", created_at="2026-01-01")) is TemporalRole.CURRENT
    assert temporal_role(FactTemporal()) is TemporalRole.UNKNOWN


def test_format_temporal_provenance_separates_happened_and_learned() -> None:
    s = format_temporal_provenance(FactTemporal(valid_at="2026-01-05", created_at="2026-01-07"))
    assert "happened 2026-01-05" in s and "learned 2026-01-07" in s
    assert "current" in s


def test_format_temporal_provenance_historical_shows_expiry() -> None:
    f = FactTemporal(valid_at="2026-01-07", created_at="2026-01-07", expired_at="2026-01-08")
    s = format_temporal_provenance(f)
    assert "no longer believed since 2026-01-08" in s and "historical" in s


def test_format_temporal_provenance_empty() -> None:
    assert format_temporal_provenance(FactTemporal()) == "no temporal information"


def test_order_by_world_time_is_ingestion_order_independent() -> None:
    """The RimWorld scrambled-ingestion case: facts learned (created_at) out of order
    still produce the correct chronological timeline by valid_at."""
    # ingestion order != world order (created_at scrambled vs valid_at)
    facts = [
        ("D_fix", FactTemporal(valid_at="2026-01-09", created_at="2026-01-09")),   # ingested 4th
        ("B_patch", FactTemporal(valid_at="2026-01-07", created_at="2026-01-05")),  # ingested 1st
        ("C_cause", FactTemporal(valid_at="2026-01-08", created_at="2026-01-06")),  # ingested 2nd
        ("A_crash", FactTemporal(valid_at="2026-01-05", created_at="2026-01-08")),  # ingested 3rd
    ]
    assert order_by_world_time(facts) == ["A_crash", "B_patch", "C_cause", "D_fix"]


def test_order_by_world_time_missing_valid_at_sorts_last() -> None:
    facts = [("x", FactTemporal()), ("a", FactTemporal(valid_at="2026-01-01"))]
    assert order_by_world_time(facts) == ["a", "x"]
