from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest

from menhir.domain.scalar_dependency_evidence import (
    DependencyEdge,
    MarkerEvidence,
    ScalarCueEvidence,
    ScalarDependencyEvidence,
    SourceBoundSpan,
    TokenEvidence,
    source_slice_sha256,
)
from menhir.services.research_scalar_dependency_rules import compose_dependency_scalar_identity
from menhir.services.typed_scalar_rules import TypedScalarProposal


SOURCE = "I have 4 lanterns"
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64


def _span(start: int, end: int, token_start: int | None = None, token_end: int | None = None) -> SourceBoundSpan:
    return SourceBoundSpan(start, end, source_slice_sha256(SOURCE, start, end), token_start, token_end)


def _span_for(source: str, start: int, end: int, token_start: int | None = None, token_end: int | None = None) -> SourceBoundSpan:
    return SourceBoundSpan(start, end, source_slice_sha256(source, start, end), token_start, token_end)


def _proposal(**overrides: object) -> TypedScalarProposal:
    values: dict[str, object] = {
        "subject_text": "I",
        "attribute": "wrong_attribute",
        "scope": "",
        "value_kind": "count",
        "unit": "",
        "operation": "absolute",
        "value": 4,
        "stated_span": SOURCE,
        "episode_uuid": "episode-1",
        "span_start": 0,
        "span_end": len(SOURCE),
        "when": None,
    }
    values.update(overrides)
    return TypedScalarProposal(**values)


def _evidence() -> ScalarDependencyEvidence:
    return _case(SOURCE)[0]


def _case(source: str, value: int = 4) -> tuple[ScalarDependencyEvidence, TypedScalarProposal]:
    words = source.rstrip(".").split()
    predicate, number, target = words[1:4]
    predicate_start = source.index(predicate)
    number_start = source.index(number, predicate_start)
    target_start = source.index(target, number_start)
    target_end = target_start + len(target)
    tokens = (
        TokenEvidence(0, _span_for(source, 0, 1, 0, 1), 1, "nsubj", "PRON", HASH_A),
        TokenEvidence(1, _span_for(source, predicate_start, predicate_start + len(predicate), 1, 2), -1, "ROOT", "VERB", HASH_A),
        TokenEvidence(2, _span_for(source, number_start, number_start + len(number), 2, 3), 3, "nummod", "NUM", HASH_A),
        TokenEvidence(3, _span_for(source, target_start, target_end, 3, 4), 1, "dobj", "NOUN", HASH_A),
    )
    evidence = ScalarDependencyEvidence(
        schema_version="scalar-dependency-v1", evidence_version="parser-evidence-v1",
        parser_id="spacy", parser_version="3.8.14", model_hash=HASH_A, pipeline_hash=HASH_B,
        source_hash=hashlib.sha256(source.encode()).hexdigest(), source_length=len(source), candidate_hash=HASH_B,
        clause_span=(
            _span_for(source, 0, len(source), 0, 4)
            if target_end == len(source)
            else _span_for(source, 0, len(source))
        ),
        tokens=tokens,
        edges=(DependencyEdge(1, 0, "nsubj"), DependencyEdge(3, 2, "nummod"), DependencyEdge(1, 3, "dobj")),
        cues=ScalarCueEvidence(
            subject=_span_for(source, 0, 1, 0, 1),
            predicate=_span_for(source, predicate_start, predicate_start + len(predicate), 1, 2),
            numeric_value=_span_for(source, number_start, number_start + len(number), 2, 3),
            unit=None, target=_span_for(source, target_start, target_end, 3, 4), modifiers=(), scope=None,
            clause_root_token=1,
        ), markers=(),
    )
    proposal = _proposal(stated_span=source, span_end=len(source), value=value)
    return evidence, proposal


def _compose(evidence: object = None, proposal: object = None, source: str = SOURCE):
    candidate = _proposal() if proposal is None else proposal
    return compose_dependency_scalar_identity(
        _evidence() if evidence is None else evidence,
        source,
        candidate,
        expected_candidate_hash=HASH_B,
        expected_parser_id="spacy",
        expected_parser_version="3.8.14",
        expected_model_hash=HASH_A,
        expected_pipeline_hash=HASH_B,
        expected_episode_uuid="episode-1",
        expected_source_key=candidate.source_key,
    )


def test_absolute_rule_composes_source_target_not_proposal_attribute() -> None:
    result = _compose()
    assert result.composed
    assert result.identity is not None
    assert result.identity.relation_type == "quantity"
    assert result.identity.target_or_scope == ("lanterns", "")
    assert result.identity.provenance.derivation_kind == "research_dependency_rule"
    assert result.receipt.outcome == "composed"
    assert SOURCE not in repr(result.receipt)
    assert "wrong_attribute" not in repr(result.receipt)


def test_marker_evidence_abstains_before_identity_composition() -> None:
    marker = MarkerEvidence("negation", _span(2, 6, 1, 2), (1,))
    result = _compose(replace(_evidence(), markers=(marker,), evidence_sha256=""))
    assert not result.composed
    assert result.receipt.reason == "markers_present"


def test_allowed_present_predicates_compose() -> None:
    for predicate in ("have", "own", "hold", "keep", "store"):
        source = f"I {predicate} 4 lanterns"
        evidence, proposal = _case(source)
        result = _compose(evidence=evidence, proposal=proposal, source=source)
        assert result.composed, (predicate, result.receipt.reason)
        assert result.identity is not None
        assert result.identity.target_or_scope == ("lanterns", "")


def test_single_token_subset_target_abstains() -> None:
    source = "I have 4 other"
    evidence, proposal = _case(source)
    result = _compose(evidence=evidence, proposal=proposal, source=source)
    assert not result.composed
    assert result.receipt.reason == "target_not_specific"


def test_zero_count_and_open_world_regular_plural_compose() -> None:
    for source in ("I have 0 lanterns", "I have 0 lanternes"):
        evidence, proposal = _case(source, value=0)
        result = _compose(evidence=evidence, proposal=proposal, source=source)
        assert result.composed, (source, result.receipt.reason)
        assert result.identity is not None
        assert result.identity.value == "0"


def test_count_one_and_irregular_or_mass_targets_abstain() -> None:
    one_evidence, one_proposal = _case("I have 1 lantern", value=1)
    one_result = _compose(evidence=one_evidence, proposal=one_proposal, source="I have 1 lantern")
    assert not one_result.composed
    assert one_result.receipt.reason == "count_one_unsupported"

    for source in ("I have 0 children", "I have 0 data"):
        evidence, proposal = _case(source, value=0)
        result = _compose(evidence=evidence, proposal=proposal, source=source)
        assert not result.composed
        assert result.receipt.reason == "target_not_plural"


@pytest.mark.parametrize(
    ("source", "reason"),
    [
        ("I have 4 quickly", "target_not_plural"),
        ("I have 4 quicklys", "target_plural_suffix_invalid"),
        ("I have 4 always", "target_not_specific"),
        ("I have 4 status", "target_plural_suffix_invalid"),
        ("I have 4 analysis", "target_plural_suffix_invalid"),
        ("I have 4 us", "target_not_specific"),
        ("I have 4 today", "target_not_specific"),
        ("I have 4 formerly", "target_not_specific"),
        ("I have 4 can", "target_not_specific"),
        ("I have 4 actually", "target_not_specific"),
        ("I have 4 correction", "target_not_specific"),
    ],
)
def test_source_target_grammar_rejects_forged_empty_marker_targets(source: str, reason: str) -> None:
    evidence, proposal = _case(source)
    result = _compose(evidence=evidence, proposal=proposal, source=source)
    assert not result.composed
    assert result.receipt.reason == reason


@pytest.mark.parametrize(
    "source",
    [
        "I have 4 lanterns and 5 maps",
        "I have 4 lanterns today",
        "I have 4 lanterns, actually",
    ],
)
def test_source_extra_claim_tokens_abstain_even_without_markers(source: str) -> None:
    evidence, proposal = _case(source)
    result = _compose(evidence=evidence, proposal=proposal, source=source)
    assert not result.composed


def _fabricated_reordered_evidence() -> ScalarDependencyEvidence:
    source = "I 4 have lanterns"
    tokens = (
        TokenEvidence(0, _span_for(source, 0, 1, 0, 1), 2, "nsubj", "PRON", HASH_A),
        TokenEvidence(1, _span_for(source, 2, 3, 1, 2), 3, "nummod", "NUM", HASH_A),
        TokenEvidence(2, _span_for(source, 4, 8, 2, 3), -1, "ROOT", "VERB", HASH_A),
        TokenEvidence(3, _span_for(source, 9, 17, 3, 4), 2, "dobj", "NOUN", HASH_A),
    )
    return ScalarDependencyEvidence(
        schema_version="scalar-dependency-v1", evidence_version="parser-evidence-v1",
        parser_id="spacy", parser_version="3.8.14", model_hash=HASH_A, pipeline_hash=HASH_B,
        source_hash=hashlib.sha256(source.encode()).hexdigest(), source_length=len(source), candidate_hash=HASH_B,
        clause_span=_span_for(source, 0, len(source), 0, 4), tokens=tokens,
        edges=(DependencyEdge(2, 0, "nsubj"), DependencyEdge(3, 1, "nummod"), DependencyEdge(2, 3, "dobj")),
        cues=ScalarCueEvidence(
            subject=_span_for(source, 0, 1, 0, 1), predicate=_span_for(source, 4, 8, 2, 3),
            numeric_value=_span_for(source, 2, 3, 1, 2), unit=None,
            target=_span_for(source, 9, 17, 3, 4), modifiers=(), scope=None, clause_root_token=2,
        ),
        markers=(),
    )


def test_reordered_source_does_not_compose_from_parser_topology() -> None:
    source = "I 4 have lanterns"
    evidence = _fabricated_reordered_evidence()
    proposal = _proposal(stated_span=source, span_end=len(source), value=4)
    result = _compose(evidence=evidence, proposal=proposal, source=source)
    assert not result.composed
    assert result.receipt.reason == "source_order_invalid"


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"operation": "delta"}, "proposal_constraints_invalid"),
        ({"value_kind": "measurement"}, "proposal_value_invalid"),
        ({"unit": "kg"}, "proposal_constraints_invalid"),
        ({"scope": "work"}, "proposal_constraints_invalid"),
        ({"subject_text": "you"}, "subject_not_canonical_self"),
        ({"value": 5}, "numeric_value_mismatch"),
    ],
)
def test_proposal_constraints_and_source_value_must_match(overrides: dict[str, object], reason: str) -> None:
    evidence, proposal = _case(SOURCE)
    proposal = replace(proposal, **overrides)
    result = _compose(evidence=evidence, proposal=proposal)
    assert not result.composed
    assert result.receipt.reason == reason


@pytest.mark.parametrize("source", ["I baked 4 lanterns", "I held 4 lanterns", "I hear Fara has 4 badges"])
def test_event_past_and_attribution_predicates_abstain(source: str) -> None:
    evidence, proposal = _case(source)
    result = _compose(evidence=evidence, proposal=proposal, source=source)
    assert not result.composed


def test_decimal_and_extra_numeric_values_abstain() -> None:
    decimal_source = "I have 4.5 lanterns"
    evidence, proposal = _case(decimal_source, value=4)
    assert not _compose(evidence=evidence, proposal=proposal, source=decimal_source).composed

    extra_source = "I have 4 lanterns 6"
    evidence, proposal = _case(extra_source)
    assert not _compose(evidence=evidence, proposal=proposal, source=extra_source).composed


def test_comma_integer_is_the_single_supported_numeric_form() -> None:
    source = "I have 4,000 lanterns"
    evidence, proposal = _case(source, value=4000)
    result = _compose(evidence=evidence, proposal=proposal, source=source)
    assert result.composed
    assert result.identity is not None
    assert result.identity.value == "4000"


def test_expected_parser_provenance_mismatch_abstains_before_rule() -> None:
    result = compose_dependency_scalar_identity(
        _evidence(), SOURCE, _proposal(),
        expected_candidate_hash=HASH_B,
        expected_parser_id="other-parser",
        expected_parser_version="3.8.14",
        expected_model_hash=HASH_A,
        expected_pipeline_hash=HASH_B,
        expected_episode_uuid="episode-1",
        expected_source_key=_proposal().source_key,
    )
    assert not result.composed
    assert result.receipt.reason == "bridge_validation_failed"
