"""Agreement-matching primitives for the deterministic scalar shadow, extracted from
`typed_scalar_service`. Bounded telemetry payload builders live in
`typed_scalar_service_shadow_report`; both are re-exported by
`menhir.services.typed_scalar_service`, which remains the public import surface.
"""

from __future__ import annotations

import re
from typing import Callable

from menhir.domain.scalar_identity import CompositionalScalarIdentity
from menhir.services.structural_scalar_composer import STRUCTURAL_REASON_CODES
from menhir.services.typed_scalar_rules import (
    TypedScalarProposal,
    _interpretation_label,
)

_COMPOSITION_ERROR = "struct.composer_error"
_COMPOSITIONAL_STATUSES = frozenset({
    "compositional_exact",
    "compositional_aligned",
    "identity_disagreement",
    "unresolved",
})
_MISMATCH_DIMENSIONS = (
    "subject",
    "relation",
    "target",
    "scope",
    "value_kind",
    "value",
    "unit",
    "operation",
    "effective_time",
)
_COMPOSITIONAL_REASON_CODES = STRUCTURAL_REASON_CODES | {_COMPOSITION_ERROR}


def _spans_overlap(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    """Return whether two non-empty located spans share at least one character."""
    return a_start < b_end and b_start < a_end


def _verified_shadow_alignment(
    det: TypedScalarProposal,
    llm: TypedScalarProposal,
) -> bool:
    """Verify that two proposal locators share the same real source substring."""
    if det.episode_uuid != llm.episode_uuid:
        return False
    if not _spans_overlap(det.span_start, det.span_end, llm.span_start, llm.span_end):
        return False
    common_start = max(det.span_start, llm.span_start)
    common_end = min(det.span_end, llm.span_end)
    spans: list[str] = []
    for proposal in (det, llm):
        if len(proposal.stated_span) != proposal.span_end - proposal.span_start:
            return False
        offset = common_start - proposal.span_start
        spans.append(proposal.stated_span[offset:offset + (common_end - common_start)])
    return spans[0] == spans[1] and bool(re.search(r"\w", spans[0], re.UNICODE))


def _exact_shadow_match(
    det: TypedScalarProposal,
    llm: TypedScalarProposal,
    *,
    canonical_self: bool = False,
) -> bool:
    """Exact agreement requires the same source locator and full interpretation."""
    return (
        det.source_key == llm.source_key
        and _interpretation_label(det, canonical_self=canonical_self)
        == _interpretation_label(llm, canonical_self=canonical_self)
    )


def _aligned_shadow_match(
    det: TypedScalarProposal,
    llm: TypedScalarProposal,
    *,
    canonical_self: bool = False,
) -> bool:
    """Aligned agreement tolerates quote-boundary drift through a verifiable common span.

    Pairwise overlap alone is too permissive: two claims can touch only at punctuation, or have
    synthetic offsets that do not describe the quoted text. The gate's span-alignment contract
    requires the common intersection to be a real, equal source substring containing a word.
    """
    if not _verified_shadow_alignment(det, llm) or (
        _interpretation_label(det, canonical_self=canonical_self)
        != _interpretation_label(llm, canonical_self=canonical_self)
    ):
        return False
    return True


def _matched_shadow_pairs(
    deterministic: list[TypedScalarProposal],
    llm_committed: list[TypedScalarProposal],
    predicate: Callable[[TypedScalarProposal, TypedScalarProposal], bool],
) -> tuple[tuple[int, int], ...]:
    """Return stable maximum one-to-one ``(det_index, llm_index)`` pairs."""
    matched_deterministic: dict[int, int] = {}

    def _augment(llm_index: int, seen: set[int]) -> bool:
        llm = llm_committed[llm_index]
        for det_index, det in enumerate(deterministic):
            if det_index in seen or not predicate(det, llm):
                continue
            seen.add(det_index)
            previous_llm = matched_deterministic.get(det_index)
            if previous_llm is None or _augment(previous_llm, seen):
                matched_deterministic[det_index] = llm_index
                return True
        return False

    for llm_index in range(len(llm_committed)):
        _augment(llm_index, set())
    return tuple(sorted(matched_deterministic.items(), key=lambda pair: pair[1]))


def _matched_llm_indices(
    deterministic: list[TypedScalarProposal],
    llm_committed: list[TypedScalarProposal],
    predicate: Callable[[TypedScalarProposal, TypedScalarProposal], bool],
) -> set[int]:
    """Return a stable maximum one-to-one match between deterministic and LLM claims.

    Agreement is a per-claim metric. Without one-to-one matching, one deterministic proposal could
    make several duplicate LLM claims look like agreements and inflate the shadow result.
    """
    return {
        llm_index
        for _det_index, llm_index in _matched_shadow_pairs(
            deterministic, llm_committed, predicate)
    }


def _identity_mismatch_dimensions(
    deterministic: CompositionalScalarIdentity,
    llm: CompositionalScalarIdentity,
) -> tuple[str, ...]:
    det_target, det_scope = deterministic.target_or_scope
    llm_target, llm_scope = llm.target_or_scope
    values = (
        (deterministic.subject, llm.subject),
        (deterministic.relation_type, llm.relation_type),
        (det_target, llm_target),
        (det_scope, llm_scope),
        (deterministic.value_kind, llm.value_kind),
        (deterministic.value, llm.value),
        (deterministic.unit, llm.unit),
        (deterministic.operation, llm.operation),
        (deterministic.effective_time, llm.effective_time),
    )
    return tuple(
        name
        for name, (det_value, llm_value) in zip(_MISMATCH_DIMENSIONS, values)
        if det_value != llm_value
    )
