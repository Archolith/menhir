"""Tests for facet derivation (menhir.domain.facet_derivation, Phase 2a)."""

from __future__ import annotations

import pytest

from menhir.domain.facet_derivation import derive_facets


@pytest.mark.unit
def test_structural_facets_from_graph_anchors() -> None:
    fs = derive_facets(
        content="fixed the floor",
        anchored_files=["src/menhir/services/recall_service.py"],
        anchored_symbols=["source_aware_floor"],
    )
    assert "src/menhir/services/recall_service.py" in fs.file
    assert "source_aware_floor" in fs.symbol


@pytest.mark.unit
def test_text_symbols_supplement_anchored_symbols() -> None:
    # snake / Pascal / SCREAMING_SNAKE symbols mentioned in prose are added alongside
    # the authoritative graph anchors.
    fs = derive_facets(
        content="RetrievalConfig reads FLOOR_EXEMPT_SOURCES via weighted_rrf",
        anchored_symbols=["source_aware_floor"],
    )
    assert {"source_aware_floor", "RetrievalConfig", "FLOOR_EXEMPT_SOURCES", "weighted_rrf"} <= fs.symbol


@pytest.mark.unit
def test_scope_and_provenance_from_metadata_win() -> None:
    fs = derive_facets(
        content="anything",
        namespace="lme-001", project="menhir", repo="archolith",
        source_id="commit:abc123", learned_time="2026-07-01T00:00:00",
    )
    assert fs.namespace == "lme-001" and fs.project == "menhir" and fs.repo == "archolith"
    assert fs.source_id == "commit:abc123" and fs.learned_time == "2026-07-01T00:00:00"


@pytest.mark.unit
def test_belief_bucket_metadata_wins_over_text() -> None:
    # explicit metadata bucket is authoritative even if text markers disagree
    fs = derive_facets(content="this is now current", belief_bucket="historical")
    assert fs.belief_bucket == "historical"


@pytest.mark.unit
def test_belief_bucket_fallback_from_text_when_metadata_absent() -> None:
    assert derive_facets(content="we used to do X").belief_bucket == "historical"
    assert derive_facets(content="currently we do Y").belief_bucket == "current"


@pytest.mark.unit
def test_interpretive_facets_from_content() -> None:
    fs = derive_facets(content="We fixed the floor; see the commit and the benchmark.")
    assert "fix" in fs.operation
    assert "commit" in fs.evidence_type and "benchmark" in fs.evidence_type
    assert "self" in fs.actor


@pytest.mark.unit
def test_valid_time_fallback_from_iso_in_content() -> None:
    assert derive_facets(content="landed on 2026-07-05 in prod").valid_time == "2026-07-05"
    assert derive_facets(content="no date here").valid_time is None


@pytest.mark.unit
def test_test_names_not_double_counted_as_symbols() -> None:
    fs = derive_facets(content="added test_recall_floor to the suite")
    assert "test_recall_floor" in fs.test
    assert "test_recall_floor" not in fs.symbol


@pytest.mark.unit
def test_anchored_test_symbol_classified_as_test_not_symbol() -> None:
    # A test name arriving from the graph (DEFINES symbol) must land in `test`, not
    # `symbol` — otherwise it never converges with a query whose test name (from prose)
    # goes to `test`. Regression for the R2 gate-(a) test/symbol facet-family mismatch.
    candidate = derive_facets(anchored_symbols=["test_stats_size_tracks_entries", "StatsTracker"])
    assert "test_stats_size_tracks_entries" in candidate.test
    assert "test_stats_size_tracks_entries" not in candidate.symbol
    assert "StatsTracker" in candidate.symbol  # non-test symbols stay in `symbol`

    # Query side derives the same test facet from prose -> the two now meet.
    query = derive_facets(content="how does test_stats_size_tracks_entries work")
    assert query.test & candidate.test == {"test_stats_size_tracks_entries"}


@pytest.mark.unit
def test_belief_bucket_word_boundaries_reject_partial_matches() -> None:
    # "now " / "current " inside other words must not classify as current.
    assert derive_facets(content="I know the answer").belief_bucket != "current"
    assert derive_facets(content="concurrent writes are safe").belief_bucket != "current"


@pytest.mark.unit
def test_belief_bucket_trailing_marker_word_is_current() -> None:
    # a sentence ENDING in "current" (no trailing space) still derives "current".
    assert derive_facets(content="the concurrency model is current").belief_bucket == "current"


@pytest.mark.unit
def test_belief_bucket_negated_historical_marker_is_not_historical() -> None:
    assert derive_facets(
        content="This API is NOT deprecated and is fully current"
    ).belief_bucket != "historical"
    assert derive_facets(
        content="do not use the deprecated flag; use the new one"
    ).belief_bucket != "historical"


@pytest.mark.unit
def test_belief_bucket_positive_controls() -> None:
    # without these, the negative assertions above would pass against a classifier
    # that returned None for everything.
    assert derive_facets(content="this API is deprecated").belief_bucket == "historical"
    assert derive_facets(content="we now use the new client").belief_bucket == "current"


@pytest.mark.unit
def test_regular_memory_has_no_structural_but_has_scope_and_interpretive() -> None:
    # A regular (unanchored) memory: no anchors, but still carries scope + interpretive
    # facets — the corpus-wide fashion.
    fs = derive_facets(content="We deprecated the old preset", project="menhir", namespace="default")
    assert fs.file == set()
    assert fs.project == "menhir"
    assert "deprecate" in fs.operation
    assert fs.belief_bucket == "historical"  # "deprecated" is a historical marker
