from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
import hashlib

import pytest

from menhir.domain.scalar_dependency_evidence import (
    DependencyEdge,
    MarkerEvidence,
    ScalarCueEvidence,
    ScalarDependencyEvidence,
    ScalarDependencyEvidenceReceipt,
    SourceBoundSpan,
    TokenEvidence,
    canonical_evidence_json,
    compute_evidence_hash,
    source_slice_sha256,
    verify_evidence_hash,
)


SOURCE = "I have 4 engineers."
HASH_A = "a" * 64
HASH_B = "b" * 64


def _span(start: int, end: int, *, token_start: int | None = None, token_end: int | None = None) -> SourceBoundSpan:
    return SourceBoundSpan(
        start=start,
        end=end,
        surface_sha256=source_slice_sha256(SOURCE, start, end),
        token_start=token_start,
        token_end=token_end,
    )


def _valid() -> ScalarDependencyEvidence:
    clause = _span(0, 18)
    tokens = (
        TokenEvidence(0, _span(0, 1, token_start=0, token_end=1), 1, "nsubj", "PRON", HASH_A),
        TokenEvidence(1, _span(2, 6, token_start=1, token_end=2), -1, "ROOT", "VERB", HASH_A),
        TokenEvidence(2, _span(7, 8, token_start=2, token_end=3), 1, "nummod", "NUM", HASH_A),
        TokenEvidence(3, _span(9, 18, token_start=3, token_end=4), 1, "obj", "NOUN", HASH_A),
    )
    cues = ScalarCueEvidence(
        subject=_span(0, 1),
        predicate=_span(2, 6),
        numeric_value=_span(7, 8),
        unit=None,
        target=_span(9, 18),
        modifiers=(),
        scope=None,
        clause_root_token=1,
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
        clause_span=clause,
        tokens=tokens,
        edges=(
            DependencyEdge(1, 0, "nsubj"),
            DependencyEdge(1, 2, "nummod"),
            DependencyEdge(1, 3, "obj"),
        ),
        cues=cues,
        markers=(),
    )


def test_valid_construction_is_deterministic_and_hash_verified() -> None:
    evidence = _valid()
    again = _valid()

    assert evidence.evidence_sha256 == compute_evidence_hash(evidence)
    assert evidence.evidence_sha256 == again.evidence_sha256
    assert canonical_evidence_json(evidence) == canonical_evidence_json(again)
    assert verify_evidence_hash(evidence)


@pytest.mark.parametrize("bad_hash", ["A" * 64, "0" * 63, "not-a-hash"])
def test_hashes_must_be_lowercase_sha256(bad_hash: str) -> None:
    with pytest.raises(ValueError):
        SourceBoundSpan(0, 1, bad_hash)


def test_evidence_and_receipt_versions_are_closed_tokens() -> None:
    with pytest.raises(ValueError):
        replace(_valid(), schema_version='"quoted"')
    with pytest.raises(ValueError):
        replace(_valid(), schema_version="engineers")
    with pytest.raises(ValueError):
        replace(_valid(), evidence_version="engineers")
    with pytest.raises(ValueError):
        replace(_valid(), parser_version="Parser 1")
    with pytest.raises(ValueError):
        ScalarDependencyEvidenceReceipt(
            schema_version="s",
            evidence_version="e",
            bridge_version='"quoted"',
            source_hash=HASH_A,
            candidate_hash=HASH_B,
            evidence_sha256=HASH_A,
            clause_start=0,
            clause_end=1,
            token_count=0,
            edge_count=0,
            marker_count=0,
            outcome="ok",
            reason="reason",
            rule_version="not_applied",
            composer_version="not_applied",
            check_names=(),
        )


def test_bool_ints_are_rejected() -> None:
    with pytest.raises(ValueError):
        SourceBoundSpan(False, 1, HASH_A)
    with pytest.raises(ValueError):
        DependencyEdge(True, 0, "dep")
    with pytest.raises(ValueError):
        ScalarDependencyEvidenceReceipt(
            "s", "e", "bridge-v1", HASH_A, HASH_B, HASH_A, False, 1, 1, 0, 0, "ok", "reason", "r", "c", ()
        )


@pytest.mark.parametrize("start,end", [(0, 0), (2, 1), (-1, 1)])
def test_spans_are_nonempty_half_open_nonnegative(start: int, end: int) -> None:
    with pytest.raises(ValueError):
        SourceBoundSpan(start, end, HASH_A)


@pytest.mark.parametrize("indices", [(0, 1, 1, 3), (0, 1, 2, 2)])
def test_token_indices_must_be_contiguous_and_unique(indices: tuple[int, ...]) -> None:
    evidence = _valid()
    tokens = tuple(replace(token, token_index=index) for token, index in zip(evidence.tokens, indices))
    with pytest.raises(ValueError, match="contiguous"):
        replace(evidence, tokens=tokens)


def test_bad_token_head_and_dependency_edge_are_rejected() -> None:
    evidence = _valid()
    with pytest.raises(ValueError, match="head_index"):
        replace(evidence, tokens=(replace(evidence.tokens[0], head_index=99), *evidence.tokens[1:]))
    with pytest.raises(ValueError, match="outside the token range"):
        replace(evidence, edges=(DependencyEdge(99, 0, "dep"),))


def test_dependency_tree_requires_one_root_matching_edges_and_canonical_order() -> None:
    evidence = _valid()
    with pytest.raises(ValueError, match="exactly one root"):
        replace(evidence, tokens=(replace(evidence.tokens[0], head_index=-1), *evidence.tokens[1:]))
    with pytest.raises(ValueError, match="root token"):
        replace(evidence, edges=(DependencyEdge(0, 1, "root"), *evidence.edges[1:]))
    with pytest.raises(ValueError, match="exactly one dependency edge"):
        replace(evidence, edges=evidence.edges[:-1])
    with pytest.raises(ValueError, match="canonically ordered"):
        replace(evidence, edges=(evidence.edges[1], evidence.edges[0], evidence.edges[2]))


def test_dependency_tree_rejects_conflicts_self_loops_cycles_and_disconnected_components() -> None:
    evidence = _valid()
    with pytest.raises(ValueError, match="canonically ordered"):
        replace(evidence, edges=(*evidence.edges, DependencyEdge(1, 0, "other")))
    with pytest.raises(ValueError, match="self-loop"):
        replace(evidence, edges=(DependencyEdge(1, 0, "nsubj"), DependencyEdge(2, 2, "loop"), evidence.edges[2]))
    cycle_tokens = (
        replace(evidence.tokens[0], head_index=2),
        evidence.tokens[1],
        replace(evidence.tokens[2], head_index=0),
        evidence.tokens[3],
    )
    cycle_edges = (
        DependencyEdge(2, 0, "nsubj"),
        DependencyEdge(0, 2, "nummod"),
        evidence.edges[2],
    )
    with pytest.raises(ValueError, match="cycle"):
        replace(evidence, tokens=cycle_tokens, edges=cycle_edges)


def test_token_source_spans_are_monotonic_and_nested_members_are_type_checked() -> None:
    evidence = _valid()
    overlapping = replace(evidence.tokens[1], span=_span(0, 6, token_start=1, token_end=2))
    with pytest.raises(ValueError, match="monotonic"):
        replace(evidence, tokens=(evidence.tokens[0], overlapping, *evidence.tokens[2:]))
    with pytest.raises(ValueError, match=r"tokens\[0\]"):
        replace(evidence, tokens=(object(), *evidence.tokens[1:]))
    with pytest.raises(ValueError, match=r"edges\[0\]"):
        replace(evidence, edges=(object(), *evidence.edges[1:]))


def test_clause_cue_and_marker_token_bounds_are_checked() -> None:
    evidence = _valid()
    with pytest.raises(ValueError, match="clause_span token bounds"):
        replace(evidence, clause_span=_span(0, 18, token_start=1, token_end=4))
    cue = replace(evidence.cues, target=_span(9, 18, token_start=4, token_end=5))
    with pytest.raises(ValueError, match="token bounds"):
        replace(evidence, cues=cue)
    marker = MarkerEvidence("negation", _span(2, 6, token_start=1, token_end=2), (4,))
    with pytest.raises(ValueError, match="token index"):
        replace(evidence, markers=(marker,))


def test_bounded_spans_align_source_offsets_with_referenced_tokens() -> None:
    evidence = _valid()
    multi_token_modifier = _span(2, 8, token_start=1, token_end=3)
    bounded_cues = replace(evidence.cues, modifiers=(multi_token_modifier,))
    assert replace(evidence, cues=bounded_cues, evidence_sha256="").cues.modifiers == (multi_token_modifier,)

    mismatched_cue = replace(evidence.cues, subject=_span(0, 1, token_start=3, token_end=4))
    with pytest.raises(ValueError, match="do not align"):
        replace(evidence, cues=mismatched_cue)

    mismatched_marker = MarkerEvidence(
        "negation", _span(2, 6, token_start=3, token_end=4), (3,)
    )
    with pytest.raises(ValueError, match="do not align"):
        replace(evidence, markers=(mismatched_marker,))


def test_marker_indices_and_marker_order_are_canonical() -> None:
    evidence = _valid()
    with pytest.raises(ValueError, match="sorted and unique"):
        MarkerEvidence("negation", _span(2, 6, token_start=1, token_end=3), (1, 1))
    marker_a = MarkerEvidence("a", _span(2, 6, token_start=1, token_end=2), (1,))
    marker_b = MarkerEvidence("b", _span(0, 1, token_start=0, token_end=1), (0,))
    with pytest.raises(ValueError, match="canonically ordered"):
        replace(evidence, markers=(marker_a, marker_b))


def test_modifier_spans_must_be_in_source_order() -> None:
    evidence = _valid()
    cue = replace(evidence.cues, modifiers=(_span(9, 18), _span(2, 6)))
    with pytest.raises(ValueError, match="modifier"):
        replace(evidence, cues=cue)


def test_cue_and_marker_spans_must_be_inside_clause() -> None:
    evidence = _valid()
    outside = _span(18, 19)
    cues = replace(evidence.cues, target=outside)
    with pytest.raises(ValueError, match="cues.target"):
        replace(evidence, cues=cues)
    marker = MarkerEvidence("negation", outside)
    with pytest.raises(ValueError, match=r"markers\[0\]"):
        replace(evidence, markers=(marker,))


def test_transport_count_bounds_are_enforced() -> None:
    evidence = _valid()
    too_many_tokens = tuple(replace(evidence.tokens[0], token_index=index) for index in range(513))
    with pytest.raises(ValueError, match="tokens exceeds"):
        replace(evidence, tokens=too_many_tokens)
    with pytest.raises(ValueError, match="edges exceeds"):
        replace(evidence, edges=tuple(evidence.edges) * 342)
    with pytest.raises(ValueError, match="markers exceeds"):
        replace(evidence, markers=tuple(MarkerEvidence("m", _span(2, 3)) for _ in range(129)))


def test_frozen_models_reject_mutation() -> None:
    evidence = _valid()
    with pytest.raises(FrozenInstanceError):
        evidence.source_length = 1  # type: ignore[misc]


def test_hash_tampering_is_rejected() -> None:
    with pytest.raises(ValueError, match="does not match"):
        replace(_valid(), evidence_sha256="c" * 64)


def test_stale_hash_requires_explicit_clear_for_rehash() -> None:
    evidence = _valid()
    with pytest.raises(ValueError, match="does not match"):
        replace(evidence, source_length=evidence.source_length + 1)
    rehashed = replace(evidence, source_length=evidence.source_length + 1, evidence_sha256="")
    assert rehashed.evidence_sha256 == compute_evidence_hash(rehashed)


@pytest.mark.parametrize(
    "field,value",
    [
        ("parser_id", "p" * 65),
        ("parser_version", "v" * 65),
    ],
)
def test_parser_strings_are_bounded_and_trimmed(field: str, value: str) -> None:
    with pytest.raises(ValueError):
        replace(_valid(), **{field: value})
    with pytest.raises(ValueError):
        replace(_valid(), **{field: " padded"})


def test_receipt_strings_counts_and_clause_bounds_are_bounded() -> None:
    evidence = _valid()
    kwargs = dict(
        schema_version="s",
        evidence_version="e",
        bridge_version="bridge-v1",
        source_hash=evidence.source_hash,
        candidate_hash=evidence.candidate_hash,
        evidence_sha256=evidence.evidence_sha256,
        clause_start=0,
        clause_end=18,
        token_count=4,
        edge_count=3,
        marker_count=0,
        outcome="abstain",
        reason="reason",
        rule_version="rules-v1",
        composer_version="composer-v1",
        check_names=("check",),
    )
    with pytest.raises(ValueError):
        ScalarDependencyEvidenceReceipt(**{**kwargs, "reason": "r" * 257})
    with pytest.raises(ValueError):
        ScalarDependencyEvidenceReceipt(**{**kwargs, "clause_end": 1_000_001})


def test_source_slice_hash_and_serialized_receipt_are_quote_free() -> None:
    assert source_slice_sha256(SOURCE, 7, 8) == hashlib.sha256(b"4").hexdigest()
    evidence = _valid()
    receipt = ScalarDependencyEvidenceReceipt(
        schema_version=evidence.schema_version,
        evidence_version=evidence.evidence_version,
        bridge_version="bridge-v1",
        source_hash=evidence.source_hash,
        candidate_hash=evidence.candidate_hash,
        evidence_sha256=evidence.evidence_sha256,
        clause_start=evidence.clause_span.start,
        clause_end=evidence.clause_span.end,
        token_count=len(evidence.tokens),
        edge_count=len(evidence.edges),
        marker_count=len(evidence.markers),
        outcome="abstain",
        reason="protected marker",
        rule_version="rules-v1",
        composer_version="composer-v1",
        check_names=("source_hash", "target_literal"),
    )
    assert "I have 4 engineers" not in canonical_evidence_json(evidence)
    assert "I have 4 engineers" not in repr(receipt)
