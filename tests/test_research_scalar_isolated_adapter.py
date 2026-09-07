"""Opt-in mapped-surface research adapter tests."""

from types import SimpleNamespace
import re

import pytest

from menhir.services.research_scalar_adapter import adapt_research_candidate
from menhir.services.research_scalar_isolated_adapter import (
    RESEARCH_ISOLATED_ADAPTER_VERSION,
    _mapped_span,
    adapt_isolated_research_candidate,
)


def _episode(content: str):
    return [SimpleNamespace(uuid="episode-1", content=content, reference_time=None)]


def _candidate(source: str, value=3):
    return {
        "subject": "user",
        "attribute": "bikes",
        "scope": "",
        "value_kind": "count",
        "unit": "",
        "operation": "absolute",
        "value": value,
        "stated_span": source,
        "episode_uuid": "episode-1",
    }


@pytest.mark.parametrize(
    ("source", "normalized", "rule", "value"),
    [
        ("I have 3 bikes", "I have 3 bikes", "canonical", 3),
        ("I hav 3 bikes", "I have 3 bikes", "normalized_mapped", 3),
        ("I've 3 bikes", "I have 3 bikes", "normalized_mapped", 3),
        ("I've got 3 bikes", "I have 3 bikes", "normalized_mapped", 3),
        ("I have got 3 bikes", "I have 3 bikes", "normalized_mapped", 3),
        ("I've completed 20 videos so far", "I have completed 20 videos so far", "normalized_mapped", 20),
        ("I've finished 7 lessons to date", "I have finished 7 lessons to date", "normalized_mapped", 7),
        ("I've closed 40 tickets so far", "I have closed 40 tickets so far", "normalized_mapped", 40),
        ("I have have 3 bikes", "I have 3 bikes", "normalized_mapped", 3),
        ("I have three bikes", "I have 3 bikes", "normalized_mapped", 3),
        ("um, I have 3 bikes", "I have 3 bikes", "normalized_mapped", 3),
        ("erm I have 3 bikes", "I have 3 bikes", "normalized_mapped", 3),
    ],
)
def test_clean_and_noisy_pairs_preserve_original_proposal_and_target(source, normalized, rule, value):
    candidate = _candidate(source, value=value)
    result = adapt_isolated_research_candidate(candidate, _episode(source))
    base = adapt_research_candidate(candidate, _episode(source))

    assert result.proposal == base.proposal
    assert result.proposal is not None
    assert result.receipt.mode == rule
    if rule == "canonical":
        assert result.composition == base.composition
    assert result.isolation is not None
    assert result.isolation.normalized_text == normalized
    assert result.composition is not None and result.composition.composed
    assert result.composition.identity is not None
    expected_target = re.search(r"\b\d+\s+([a-z][a-z0-9'-]*)", normalized).group(1)
    assert result.composition.identity.target_or_scope[0] == expected_target
    assert result.composition.identity.provenance.source_key == result.proposal.source_key
    assert result.composition.identity.provenance.derivation_kind == (
        "research_normalized_structural_grammar" if rule == "normalized_mapped" else "structural_grammar"
    )
    assert result.composition.identity.provenance.derivation_version == (
        RESEARCH_ISOLATED_ADAPTER_VERSION if rule == "normalized_mapped" else "structural-v4"
    )
    assert result.composition.receipt.target_text == expected_target
    assert source[result.composition.receipt.target_start:result.composition.receipt.target_end] == expected_target


@pytest.mark.parametrize(
    "source",
    [
        "I don't have 3 bikes",
        "I got 3 bikes",
        "I used to have 3 bikes",
        "I bought 3 bikes",
        "I have 3 other bikes",
        "um maybe I have 3 bikes",
        "I have um 3 bikes",
        "I got two—no, three bikes",
        "I have 3 bikes and 2 books",
        "I've completed 3 other lessons so far",
        "I've finished 3 more tasks to date",
        "I've closed 3 additional tickets so far",
        "I've completed 3 ones so far",
        "I've finished 3 items to date",
        "I've closed 3 stuff so far",
        "I've completed 3 total tasks so far",
        "I've completed 3 lessons since January",
        "I've finished 3 tasks last year",
        "I've completed 3 lessons and 2 tests so far",
        "I've completed 3 lessons up to now",
    ],
)
def test_protected_roles_corrections_and_competing_numbers_never_compose(source):
    candidate = _candidate(source, value=3)
    result = adapt_isolated_research_candidate(candidate, _episode(source))

    assert result.composition is None
    assert result.receipt.outcome in {"abstained", "rejected"}
    assert result.receipt.mode != "normalized_mapped"


def test_mapping_receipt_is_bounded_and_quote_free():
    source = "I hav 3 bikes"
    result = adapt_isolated_research_candidate(_candidate(source), _episode(source))

    assert result.receipt.mapped_cues
    assert source not in repr(result.receipt)
    assert result.receipt.source_key == result.proposal.source_key
    assert all(
        cue.source_start >= result.proposal.span_start
        and cue.source_end <= result.proposal.span_end
        for cue in result.receipt.mapped_cues
    )


def test_completed_mapping_receipt_proves_relation_and_cumulative_marker():
    source = "I've completed 20 videos so far"
    result = adapt_isolated_research_candidate(
        _candidate(source, value=20),
        _episode(source),
    )

    assert result.receipt.mode == "normalized_mapped"
    assert [cue.kind for cue in result.receipt.mapped_cues] == [
        "relation",
        "value",
        "target",
        "cumulative",
    ]
    cues = {cue.kind: cue for cue in result.receipt.mapped_cues}
    assert source[cues["relation"].source_start:cues["relation"].source_end] == "completed"
    assert source[cues["value"].source_start:cues["value"].source_end] == "20"
    assert source[cues["target"].source_start:cues["target"].source_end] == "videos"
    assert source[cues["cumulative"].source_start:cues["cumulative"].source_end] == "so far"
    assert source not in repr(result.receipt)


def test_source_authoritative_count_survives_normalization_and_stays_quote_free():
    source = "I hav 3 bikes"
    result = adapt_isolated_research_candidate(_candidate(source, value=4), _episode(source))

    assert result.receipt.mode == "normalized_mapped"
    assert result.proposal is not None
    assert result.proposal.value == 3
    assert result.receipt.normalization_rules == ("spelling_hav_have",)
    assert result.isolation is not None
    assert result.receipt.normalization_rules == result.isolation.receipt.normalization_rules
    assert source not in repr(result.receipt)


def test_protected_abstention_forwards_normalization_rules():
    source = "I don't hav 3 bikes"
    result = adapt_isolated_research_candidate(_candidate(source), _episode(source))

    assert result.receipt.mode == "protected_role"
    assert result.receipt.normalization_rules == ("spelling_hav_have",)
    assert source not in repr(result.receipt)


def test_repeated_have_alignment_keeps_literal_target_provenance():
    source = "I have have three bikes"
    result = adapt_isolated_research_candidate(_candidate(source), _episode(source))

    assert result.receipt.mode == "normalized_mapped"
    assert result.composition is not None and result.composition.composed
    assert result.composition.receipt.target_text == "bikes"
    assert result.composition.identity is not None
    assert result.composition.identity.provenance.source_key == result.proposal.source_key


def test_literal_mapping_rejects_noncontiguous_or_reordered_ranges():
    assert _mapped_span(((0, 1), (2, 3)), 0, 2, require_literal=True) is None
    assert _mapped_span(((1, 2), (0, 1)), 0, 2, require_literal=True) is None
    assert _mapped_span(((0, 1), (1, 2)), 0, 2, require_literal=True) == (0, 2)
