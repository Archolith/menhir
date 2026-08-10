from __future__ import annotations

from dataclasses import replace
import hashlib
from pathlib import Path

import pytest

from menhir.domain.scalar_dependency_evidence import (
    DependencyEdge,
    ScalarCueEvidence,
    ScalarDependencyEvidence,
    SourceBoundSpan,
    TokenEvidence,
    source_slice_sha256,
)
from menhir.services.research_scalar_dependency_bridge import (
    BRIDGE_VERSION,
    validate_source_bound_dependency_evidence,
)
from menhir.services.typed_scalar_rules import TypedScalarProposal


SOURCE = "I have 4 engineers."
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64


def _span(start: int, end: int) -> SourceBoundSpan:
    return SourceBoundSpan(start, end, source_slice_sha256(SOURCE, start, end))


def _proposal(**overrides: object) -> TypedScalarProposal:
    values: dict[str, object] = {
        "subject_text": "user",
        "attribute": "engineer_count",
        "scope": "",
        "value_kind": "count",
        "unit": "",
        "operation": "absolute",
        "value": 4,
        "stated_span": "I have 4 engineers",
        "episode_uuid": "episode-1",
        "span_start": 0,
        "span_end": 18,
        "when": None,
    }
    values.update(overrides)
    return TypedScalarProposal(**values)


def _evidence() -> ScalarDependencyEvidence:
    tokens = (
        TokenEvidence(0, _span(0, 1), 1, "nsubj", "PRON", HASH_A),
        TokenEvidence(1, _span(2, 6), -1, "ROOT", "VERB", HASH_A),
        TokenEvidence(2, _span(7, 8), 1, "nummod", "NUM", HASH_A),
        TokenEvidence(3, _span(9, 18), 1, "obj", "NOUN", HASH_A),
    )
    return ScalarDependencyEvidence(
        schema_version="scalar-dependency-v1",
        evidence_version="parser-evidence-v1",
        parser_id="test-parser",
        parser_version="1.0.0",
        model_hash=HASH_A,
        pipeline_hash=HASH_B,
        source_hash=hashlib.sha256(SOURCE.encode()).hexdigest(),
        source_length=len(SOURCE),
        candidate_hash=HASH_B,
        clause_span=_span(0, 18),
        tokens=tokens,
        edges=(DependencyEdge(1, 0, "nsubj"), DependencyEdge(1, 2, "nummod"), DependencyEdge(1, 3, "obj")),
        cues=ScalarCueEvidence(
            subject=_span(0, 1),
            predicate=_span(2, 6),
            numeric_value=_span(7, 8),
            unit=None,
            target=_span(9, 18),
            modifiers=(),
            scope=None,
            clause_root_token=1,
        ),
        markers=(),
    )


def _validate(
    evidence: object = None,
    source: object = SOURCE,
    proposal: object = None,
    expected: object = HASH_B,
    expected_episode: object = "episode-1",
    expected_source_key: object = None,
):
    candidate = _proposal() if proposal is None else proposal
    default_source_key = candidate.source_key if isinstance(candidate, TypedScalarProposal) else _proposal().source_key
    return validate_source_bound_dependency_evidence(
        _evidence() if evidence is None else evidence,
        source,
        candidate,
        expected_candidate_hash=expected,
        expected_parser_id="test-parser",
        expected_parser_version="1.0.0",
        expected_model_hash=HASH_A,
        expected_pipeline_hash=HASH_B,
        expected_episode_uuid=expected_episode,
        expected_source_key=default_source_key if expected_source_key is None else expected_source_key,
    )


def test_happy_path_is_validated_with_bridge_metadata() -> None:
    receipt = _validate()

    assert receipt.outcome == "validated"
    assert receipt.reason == "validated_source_bound_evidence"
    assert receipt.rule_version == "not_applied"
    assert receipt.composer_version == "not_applied"
    assert receipt.bridge_version == BRIDGE_VERSION
    assert receipt.check_names[-1] == "evidence_hash"


@pytest.mark.parametrize(
    "source,reason",
    [(123, "source_type_invalid"), (SOURCE + " ", "source_length_mismatch"), ("wrong", "source_length_mismatch")],
)
def test_source_type_length_and_hash_fail_closed(source: object, reason: str) -> None:
    receipt = _validate(source=source)
    assert (receipt.outcome, receipt.reason) == ("abstained", reason)


def test_source_hash_mismatch_is_reported_after_matching_length() -> None:
    evidence = replace(_evidence(), source_hash=HASH_C, evidence_sha256="")
    receipt = _validate(evidence=evidence)
    assert receipt.reason == "source_hash_mismatch"


@pytest.mark.parametrize("expected", ["bad", HASH_C])
def test_expected_candidate_hash_is_syntax_checked_and_compared(expected: str) -> None:
    receipt = _validate(expected=expected)
    assert receipt.reason in {"expected_candidate_hash_invalid", "candidate_hash_mismatch"}


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("parser_id", "other-parser", "parser_id_mismatch"),
        ("parser_version", "1.0.1", "parser_version_mismatch"),
        ("model_hash", HASH_C, "model_hash_mismatch"),
        ("pipeline_hash", HASH_C, "pipeline_hash_mismatch"),
    ],
)
def test_expected_parser_provenance_rejects_syntactically_valid_drift(
    field: str, value: str, reason: str
) -> None:
    evidence = replace(_evidence(), **{field: value}, evidence_sha256="")
    assert _validate(evidence=evidence).reason == reason


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("expected_parser_id", "Other Parser", "expected_parser_id_invalid"),
        ("expected_parser_version", "1.0.0 ", "expected_parser_version_invalid"),
        ("expected_model_hash", "not-a-hash", "expected_model_hash_invalid"),
        ("expected_pipeline_hash", [], "expected_pipeline_hash_invalid"),
    ],
)
def test_expected_parser_provenance_rejects_malformed_expectations(
    field: str, value: object, reason: str
) -> None:
    kwargs = {field: value}
    candidate = _proposal()
    receipt = validate_source_bound_dependency_evidence(
        _evidence(),
        SOURCE,
        candidate,
        expected_candidate_hash=HASH_B,
        expected_parser_id=kwargs.get("expected_parser_id", "test-parser"),
        expected_parser_version=kwargs.get("expected_parser_version", "1.0.0"),
        expected_model_hash=kwargs.get("expected_model_hash", HASH_A),
        expected_pipeline_hash=kwargs.get("expected_pipeline_hash", HASH_B),
        expected_episode_uuid="episode-1",
        expected_source_key=candidate.source_key,
    )
    assert receipt.reason == reason


def test_forged_proposal_type_bounds_source_key_and_quote_fail_closed() -> None:
    assert _validate(proposal=object()).reason == "proposal_type_invalid"
    assert _validate(proposal=_proposal(span_start=-1)).reason == "proposal_locator_invalid"

    class ForgedProposal(TypedScalarProposal):
        @property
        def source_key(self) -> str:
            return HASH_C

    forged = ForgedProposal(**_proposal().__dict__) if hasattr(_proposal(), "__dict__") else ForgedProposal(
        subject_text="user", attribute="engineer_count", scope="", value_kind="count", unit="",
        operation="absolute", value=4, stated_span="I have 4 engineers", episode_uuid="episode-1",
        span_start=0, span_end=18, when=None,
    )
    assert _validate(proposal=forged).reason == "proposal_source_key_mismatch"
    assert _validate(proposal=_proposal(stated_span="i HAVE 4 ENGINEERS")).outcome == "validated"
    assert _validate(proposal=_proposal(stated_span="I have 4 engineers", span_start=1)).reason == "proposal_quote_mismatch"


def test_external_episode_and_source_key_expectations_are_required() -> None:
    assert _validate(expected_episode="other-episode").reason == "expected_episode_uuid_mismatch"
    assert _validate(expected_source_key=HASH_C).reason == "expected_source_key_mismatch"
    assert _validate(expected_episode="").reason == "expected_episode_uuid_invalid"
    assert _validate(expected_source_key="not-a-hash").reason == "expected_source_key_invalid"


def test_clause_and_numeric_cue_scope_mismatches_abstain() -> None:
    evidence = _evidence()
    object.__setattr__(evidence, "clause_span", _span(2, 18))
    object.__setattr__(evidence, "evidence_sha256", "")
    assert _validate(evidence=evidence).reason == "clause_not_containing_proposal"

    numeric_outside = replace(evidence.cues, numeric_value=_span(9, 18))
    evidence = replace(_evidence(), cues=numeric_outside, evidence_sha256="")
    narrow_proposal = _proposal(stated_span="I have 4", span_end=8)
    assert _validate(evidence=evidence, proposal=narrow_proposal).reason == "numeric_cue_outside_proposal"


def test_stale_surface_and_evidence_hashes_fail_closed() -> None:
    forged_target = replace(_evidence().cues.target, surface_sha256=HASH_C)
    evidence = replace(_evidence(), cues=replace(_evidence().cues, target=forged_target), evidence_sha256="")
    assert _validate(evidence=evidence).reason == "surface_hash_mismatch"

    stale = _evidence()
    object.__setattr__(stale, "evidence_sha256", HASH_C)
    assert _validate(evidence=stale).reason == "evidence_hash_mismatch"


def test_receipts_are_quote_free_deterministic_and_bounded() -> None:
    first = _validate()
    second = _validate()
    assert first == second
    assert SOURCE not in repr(first)
    assert "I have" not in repr(first)
    assert first.rule_version == first.composer_version == "not_applied"
    assert first.bridge_version == BRIDGE_VERSION


def test_malformed_evidence_and_forged_metadata_always_return_bounded_receipts() -> None:
    malformed = object()
    receipt = _validate(evidence=malformed)
    assert receipt.outcome == "abstained"
    assert len(repr(receipt)) < 2_000

    forged = _evidence()
    object.__setattr__(forged, "schema_version", '"I have 4 engineers"')
    object.__setattr__(forged.clause_span, "start", 0)
    object.__setattr__(forged.clause_span, "end", 0)
    receipt = _validate(evidence=forged)
    assert "I have 4 engineers" not in repr(receipt)
    assert receipt.schema_version == "scalar-dependency-v1"

    probe = _evidence()
    object.__setattr__(probe, "schema_version", "s" * 1_000)
    object.__setattr__(probe.clause_span, "start", 0)
    object.__setattr__(probe.clause_span, "end", 0)
    receipt = _validate(evidence=probe)
    assert receipt.schema_version == "scalar-dependency-v1"
    assert 0 <= receipt.clause_start < receipt.clause_end <= 1_000_000


def test_forged_unknown_transport_versions_abstain_without_metadata_leak() -> None:
    evidence = _evidence()
    object.__setattr__(evidence, "schema_version", "engineers")
    receipt = _validate(evidence=evidence)
    assert receipt.reason == "evidence_version_unsupported"
    assert "engineers" not in repr(receipt)


@pytest.mark.parametrize("field", ["schema_version", "evidence_version"])
@pytest.mark.parametrize("value", [[], {}, ["engineers"]])
def test_unhashable_forged_versions_return_bounded_receipts(field: str, value: object) -> None:
    evidence = _evidence()
    object.__setattr__(evidence, field, value)
    receipt = _validate(evidence=evidence)
    assert receipt.outcome == "abstained"
    assert receipt.reason == "evidence_version_unsupported"
    assert "engineers" not in repr(receipt)

    evidence = _evidence()
    object.__setattr__(evidence, "evidence_version", "engineers")
    receipt = _validate(evidence=evidence)
    assert receipt.reason == "evidence_version_unsupported"
    assert "engineers" not in repr(receipt)


def test_bridge_has_no_parser_runtime_or_composition_imports() -> None:
    text = Path("src/menhir/services/research_scalar_dependency_bridge.py").read_text(encoding="utf-8")
    assert "spacy" not in text.lower()
    assert "compositional_scalar_identity" not in text
    assert "typed_scalar_persistence" not in text
