"""Unit tests for the post-LLM dedup identity gate.

Tests _has_positive_identity_evidence — the function that decides whether
a merge proposed by the dedup LLM should be allowed or vetoed.
"""

from __future__ import annotations

import pytest

from menhir.infrastructure.graphiti_patches import (
    _edge_facts_mention,
    _has_positive_identity_evidence,
)


# ── Should ALLOW (positive evidence exists) ──


@pytest.mark.unit
@pytest.mark.parametrize(
    "extracted, candidate",
    [
        # Exact match
        ("Chicago", "Chicago"),
        ("chicago", "Chicago"),
        # Substring
        ("NYC", "NYC metro area"),
        ("New York City", "New York City"),
        ("Bob", "Bobby"),
        # Acronym
        ("IBM", "International Business Machines"),
        ("ibm", "International Business Machines"),
        ("NASA", "National Aeronautics Space Administration"),
        # Token overlap
        ("New York City", "New York Knicks"),  # 2/4 = 50% overlap
        ("Sam Smith", "Sam Smith Jr"),
    ],
    ids=[
        "exact",
        "exact-case-insensitive",
        "substring-short-in-long",
        "substring-identical",
        "substring-bob-bobby",
        "acronym-IBM",
        "acronym-ibm-lower",
        "acronym-NASA",
        "token-overlap-new-york",
        "token-overlap-sam-smith",
    ],
)
def test_positive_evidence_allows_merge(extracted: str, candidate: str) -> None:
    assert _has_positive_identity_evidence(extracted, candidate) is True


# ── Should BLOCK (no positive evidence) ──


@pytest.mark.unit
@pytest.mark.parametrize(
    "extracted, candidate",
    [
        # The exact failure mode: descriptive location vs named city
        ("the suburbs", "Chicago"),
        ("downtown", "San Francisco"),
        ("the countryside", "Paris"),
        # Completely unrelated
        ("Python", "Basketball"),
        ("Alice", "Wonderland"),
        # Related but distinct — no lexical overlap
        ("suburbs", "Chicago"),
        ("the beach", "Miami"),
        # Edge cases
        ("", "Chicago"),
        ("the suburbs", ""),
    ],
    ids=[
        "suburbs-vs-chicago",
        "downtown-vs-sf",
        "countryside-vs-paris",
        "unrelated-python-basketball",
        "unrelated-alice-wonderland",
        "suburbs-no-article-vs-chicago",
        "beach-vs-miami",
        "empty-extracted",
        "empty-candidate",
    ],
)
def test_no_evidence_blocks_merge(extracted: str, candidate: str) -> None:
    assert _has_positive_identity_evidence(extracted, candidate) is False


# ── Boundary cases ──


@pytest.mark.unit
def test_short_substring_not_matched() -> None:
    """Substrings shorter than 3 chars should not count as evidence."""
    # "am" is in "Sam" but too short to be meaningful
    assert _has_positive_identity_evidence("am", "Sam") is False


@pytest.mark.unit
def test_stopwords_excluded_from_token_overlap() -> None:
    """Stopwords like 'the', 'a', 'of' should not create false positive overlap."""
    # Only shared token is "the" which is a stopword
    assert _has_positive_identity_evidence("the suburbs", "the city") is False


@pytest.mark.unit
def test_acronym_reversed() -> None:
    """Acronym matching works in both directions."""
    assert _has_positive_identity_evidence("International Business Machines", "IBM") is True


# ── Edge-consistency invariant tests ──


class _FakeEdge:
    """Minimal edge stub with a .fact attribute."""
    def __init__(self, fact: str) -> None:
        self.fact = fact


@pytest.mark.unit
def test_edge_facts_mention_finds_matching_facts() -> None:
    edges = [
        _FakeEdge("Rachel moved back to the suburbs again."),
        _FakeEdge("Rachel lives in Chicago."),
    ]
    result = _edge_facts_mention("suburbs", edges)
    assert result == {"Rachel moved back to the suburbs again."}


@pytest.mark.unit
def test_edge_facts_mention_case_insensitive() -> None:
    edges = [_FakeEdge("Rachel lives in CHICAGO.")]
    result = _edge_facts_mention("chicago", edges)
    assert result == {"Rachel lives in CHICAGO."}


@pytest.mark.unit
def test_edge_facts_mention_no_match() -> None:
    edges = [_FakeEdge("Rachel lives in Chicago.")]
    result = _edge_facts_mention("suburbs", edges)
    assert result == set()


@pytest.mark.unit
def test_edge_facts_mention_none_edges() -> None:
    assert _edge_facts_mention("suburbs", None) == set()


@pytest.mark.unit
def test_edge_facts_mention_short_name_skipped() -> None:
    """Entity names shorter than 3 chars are skipped to avoid trivial matches."""
    edges = [_FakeEdge("I am here.")]
    assert _edge_facts_mention("am", edges) == set()


@pytest.mark.unit
def test_edge_facts_mention_dict_edges() -> None:
    """Supports dict-style edges (e.g. from JSON trace data)."""
    edges = [{"fact": "Rachel moved to the suburbs."}]
    result = _edge_facts_mention("suburbs", edges)
    assert result == {"Rachel moved to the suburbs."}
