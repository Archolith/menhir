"""Pure research tests for conservative scalar clause/evidence isolation."""

import pytest

from menhir.services.research_scalar_clause_isolator import (
    REASON_COMPETING_NUMERIC_CLAUSES,
    REASON_CONJUNCTION_AMBIGUOUS,
    REASON_CORRECTION_UNRESOLVED,
    REASON_NO_NUMERIC_CLAUSE,
    REASON_SPAN_AMBIGUOUS,
    RESEARCH_CLAUSE_ISOLATOR_VERSION,
    isolate_research_scalar_candidate,
    isolate_research_scalar_clause,
)


def test_clean_and_noisy_pairs_share_scalar_evidence_and_preserve_offsets():
    pairs = [
        ("I have 3 bikes.", "I have 3 bikes.", ()),
        ("i hav 3 bikes", "i have 3 bikes", ("spelling_hav_have",)),
        ("I've 3 bikes", "I have 3 bikes", ("contraction_ive",)),
        ("I've got 3 bikes", "I have 3 bikes", ("contraction_ive", "lexical_possession_got")),
        ("I have got 3 bikes", "I have 3 bikes", ("lexical_possession_got",)),
        ("I have have 3 bikes", "I have 3 bikes", ("duplicate_have",)),
        ("I have three bikes", "I have 3 bikes", ("number_word_to_digit",)),
        ("um, I have 3 bikes", "I have 3 bikes", ("leading_filler_strip",)),
        ("erm I have 3 bikes", "I have 3 bikes", ("leading_filler_strip",)),
        ("three bike rn", "3 bike rn", ("number_word_to_digit",)),
    ]
    for source, normalized, expected_rules in pairs:
        result = isolate_research_scalar_clause(source)
        assert result.receipt.outcome == "isolated"
        assert result.isolated_text == source
        assert result.normalized_text == normalized
        assert result.source_start == 0
        assert result.source_end == len(source)
        assert result.receipt.normalization_rules == expected_rules
        assert result.receipt.source_hash
        assert source not in repr(result.receipt)


def test_conjunction_separated_candidate_uses_explicit_claimed_span():
    source = "I have 3 bikes and I have 2 books."
    result = isolate_research_scalar_clause(
        source,
        stated_span="I have 3 bikes",
    )

    assert result.isolated_text == "I have 3 bikes"
    assert result.normalized_text == "I have 3 bikes"
    assert (result.source_start, result.source_end) == (0, len("I have 3 bikes"))


def test_whitespace_variant_claim_keeps_original_offsets():
    source = "I have 3 bikes."
    result = isolate_research_scalar_clause(source, stated_span="I  have   3 bikes")

    assert result.isolated_text == "I have 3 bikes"
    assert (result.source_start, result.source_end) == (0, len("I have 3 bikes"))


def test_punctuation_free_fragment_isolated_without_guessing_target_or_role():
    result = isolate_research_scalar_clause("got 3 bikes now")

    assert result.isolated_text == "got 3 bikes now"
    assert result.normalized_text == "got 3 bikes now"
    assert result.receipt.protected_roles == ("event", "temporal")


def test_offsets_can_validate_a_noisy_claim_without_rewriting_source():
    source = "I hav 3 bikes."
    result = isolate_research_scalar_clause(
        source,
        stated_span="I have 3 bikes",
        span_start=0,
        span_end=len(source.rstrip(".")),
    )

    assert result.isolated_text == "I hav 3 bikes"
    assert result.normalized_text == "I have 3 bikes"
    assert (result.source_start, result.source_end) == (0, len(source.rstrip(".")))


def test_raw_candidate_wrapper_is_non_mutating_and_uses_claimed_evidence():
    candidate = {
        "stated_span": "I have 3 bikes",
        "span_start": 0,
        "span_end": 14,
    }
    original = dict(candidate)
    result = isolate_research_scalar_candidate(candidate, "I have 3 bikes.")

    assert result.isolated_text == "I have 3 bikes"
    assert candidate == original


@pytest.mark.parametrize(
    ("source", "reason"),
    [
        ("I got two—no, three bikes", REASON_CORRECTION_UNRESOLVED),
        ("I have 3 bikes and 2 books", REASON_COMPETING_NUMERIC_CLAUSES),
        ("I have 3 bikes. I have 2 books.", REASON_COMPETING_NUMERIC_CLAUSES),
        ("I have 3 bikes and my dog", REASON_CONJUNCTION_AMBIGUOUS),
        ("No scalar claim here", REASON_NO_NUMERIC_CLAUSE),
    ],
)
def test_ambiguous_or_semantically_competing_source_fails_closed(source, reason):
    result = isolate_research_scalar_clause(source)

    assert result.isolated_text is None
    assert result.normalized_text is None
    assert result.receipt.outcome == "rejected"
    assert result.receipt.reason == reason


def test_repeated_claimed_span_is_not_silently_deduplicated():
    source = "I have 3 bikes. I have 3 bikes."
    result = isolate_research_scalar_clause(source, stated_span="I have 3 bikes")

    assert result.isolated_text is None
    assert result.receipt.reason == REASON_SPAN_AMBIGUOUS


def test_protected_roles_are_reported_without_being_erased():
    source = "I don't have 3 other bikes tomorrow."
    result = isolate_research_scalar_clause(source)

    assert result.isolated_text == source
    assert result.normalized_text == source
    assert result.receipt.protected_roles == ("negation", "subset", "temporal")
    assert result.receipt.normalization_rules == ()


def test_version_and_receipt_are_bounded_and_quote_free():
    source = "I have 3 bikes."
    result = isolate_research_scalar_clause(source)

    assert result.receipt.isolator_version == RESEARCH_CLAUSE_ISOLATOR_VERSION
    assert source not in repr(result.receipt)
    assert result.receipt.source_length == len(source)
