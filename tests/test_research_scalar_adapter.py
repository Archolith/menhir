"""Pure, offline contract tests for research scalar candidates."""

from types import SimpleNamespace

import pytest

from menhir.services.research_scalar_adapter import (
    RESEARCH_ADAPTER_VERSION,
    adapt_research_candidate,
)


def _episode(uuid: str, content: str, reference_time=None):
    return SimpleNamespace(uuid=uuid, content=content, reference_time=reference_time)


def _candidate(source: str = "I have 3 coins.", **overrides):
    row = {
        "subject": "user",
        "attribute": "coins",
        "scope": "",
        "value_kind": "count",
        "unit": "",
        "operation": "absolute",
        "value": 3,
        "stated_span": source.rstrip("."),
        "episode_uuid": "episode-1",
    }
    row.update(overrides)
    return row


def test_candidate_uuid_is_mapped_and_composed_without_open_text_receipt():
    source = "I have 3 coins."
    result = adapt_research_candidate(
        _candidate(source), [_episode("episode-1", source)], candidate_id="candidate-1"
    )

    assert result.proposal is not None
    assert result.composition is not None and result.composition.composed is True
    assert result.proposal.episode_uuid == "episode-1"
    assert result.proposal.span_start == 0
    assert result.proposal.span_end == len(source.rstrip("."))
    assert result.receipt.adapter_version == RESEARCH_ADAPTER_VERSION
    assert result.receipt.parse_status == "admitted"
    assert result.receipt.composition_status == "composed"
    assert result.receipt.source_key == result.proposal.source_key
    assert result.receipt.candidate_id_hash
    assert result.receipt.candidate_hash
    assert "stated_span" not in result.receipt.__dict__
    assert source not in repr(result.receipt)


def test_parser_rejection_is_quote_free_and_never_reaches_composer():
    source = "I have 3 coins. I have 3 coins."
    result = adapt_research_candidate(
        _candidate("I have 3 coins."), [_episode("episode-1", source)]
    )

    assert result.proposal is None
    assert result.composition is None
    assert result.receipt.parse_status == "rejected"
    assert result.receipt.parse_reason == "span_not_uniquely_located"
    assert result.receipt.composition_status is None
    assert result.receipt.source_key is None


@pytest.mark.parametrize(
    ("claims", "reason"),
    [
        ({"span_start": 1, "span_end": 15}, "claimed_span_mismatch"),
        ({"episode_uuid": "other-episode"}, "claimed_episode_uuid_mismatch"),
        ({"span_start": 0}, "claimed_span_incomplete"),
        ({"span_start": True, "span_end": 15}, "claimed_span_invalid"),
    ],
)
def test_claimed_locator_mismatch_or_malformed_claim_drops(claims, reason):
    source = "I have 3 coins."
    row = _candidate(source)
    row.update(claims)
    # Keep the parser's episode selection explicit for the UUID mismatch case.
    row.setdefault("episode", 0)
    result = adapt_research_candidate(row, [_episode("episode-1", source)])

    assert result.proposal is None
    assert result.composition is None
    assert result.receipt.parse_status == "rejected"
    assert result.receipt.parse_reason == reason


def test_composer_abstention_is_returned_without_persistence_or_routing():
    source = "My car has 4 wheels."
    row = _candidate(
        source,
        attribute="wheels",
        value=4,
        stated_span=source.rstrip("."),
    )
    result = adapt_research_candidate(row, [_episode("episode-1", source)])

    assert result.proposal is not None
    assert result.composition is not None
    assert result.composition.identity is None
    assert result.receipt.parse_status == "admitted"
    assert result.receipt.composition_status == "abstained"
    assert result.receipt.composition_reason == "struct.unbound_possessive"


@pytest.mark.parametrize(
    "row",
    [
        _candidate(value="not-a-number"),
        _candidate(value_kind="bogus"),
        _candidate(operation="bogus"),
        _candidate(when="not-an-iso-time"),
    ],
)
def test_invalid_typed_or_temporal_candidates_fail_closed(row):
    result = adapt_research_candidate(row, [_episode("episode-1", "I have 3 coins.")])

    assert result.proposal is None
    assert result.composition is None
    assert result.receipt.parse_status == "rejected"
    assert result.receipt.parse_reason


@pytest.mark.parametrize(
    ("row", "reason"),
    [
        (_candidate(operation="delta"), "struct.operation_unsupported"),
    ],
)
def test_parser_admission_still_requires_structural_composer_support(row, reason):
    result = adapt_research_candidate(row, [_episode("episode-1", "I have 3 coins.")])

    assert result.proposal is not None
    assert result.composition is not None
    assert result.composition.identity is None
    assert result.receipt.parse_status == "admitted"
    assert result.receipt.composition_status == "abstained"
    assert result.receipt.composition_reason == reason


def test_unknown_grounded_measurement_unit_fails_before_composition():
    result = adapt_research_candidate(
        _candidate(value_kind="measurement", unit="bananas"),
        [_episode("episode-1", "I have 3 coins.")],
    )

    assert result.proposal is None
    assert result.composition is None
    assert result.receipt.parse_status == "rejected"
    assert result.receipt.parse_reason == "measurement_unit_unresolved"


def test_duplicate_episode_uuids_fail_closed_before_grounding():
    source = "I have 3 coins."
    result = adapt_research_candidate(
        _candidate(source),
        [_episode("episode-1", source), _episode("episode-1", source)],
    )

    assert result.proposal is None
    assert result.receipt.parse_reason == "episode_uuid_invalid_or_duplicate"
