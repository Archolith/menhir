"""Cue mapping and mapped-surface composition for the opt-in isolated research adapter.

Extracted from ``research_scalar_isolated_adapter``: normalized cue detection over the isolated
clause, literal source span mapping, and composition of the mapped surface with original
provenance.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from menhir.domain.typed_assertion import normalize_scalar
from menhir.services.compositional_scalar_identity import compose_scalar_identity
from menhir.services.research_scalar_clause_isolator import ResearchClauseIsolation
from menhir.services.structural_scalar_composer import (
    STRUCTURAL_COMPOSER_VERSION,
    StructuralComposition,
    StructuralCompositionReceipt,
)
from menhir.services.typed_scalar_rules import TypedScalarProposal

__all__ = [
    "RESEARCH_ISOLATED_ADAPTER_VERSION",
    "ResearchMappedCue",
]


RESEARCH_ISOLATED_ADAPTER_VERSION = "research-adapter-isolated-v2"
_QUANTITY_RULE_IDS = frozenset({
    "struct.quantity.have_v1",
    "struct.quantity.completed_v1",
})
_COMPLETED_QUANTITY_RULE_ID = "struct.quantity.completed_v1"
_RESEARCH_DERIVATION_KIND = "research_normalized_structural_grammar"

_RELATION_RE = re.compile(r"\b(?:have|hold|own|possess)\b", re.IGNORECASE)
_COMPLETION_RELATION_RE = re.compile(r"\b(?:completed|finished|closed)\b", re.IGNORECASE)
_CUMULATIVE_MARKER_RE = re.compile(r"\b(?:so\s+far|to\s+date)\b", re.IGNORECASE)
_NUMBER_RE = re.compile(
    r"(?<![\w-])(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?![\w-])"
)


@dataclass(frozen=True, slots=True)
class ResearchMappedCue:
    """One normalized cue and its proven original source range."""

    kind: str
    normalized_start: int
    normalized_end: int
    source_start: int
    source_end: int
    rule: str


def _normalized_number(text: str) -> str | None:
    try:
        cleaned = text.replace(",", "")
        value: int | float = float(cleaned) if "." in cleaned else int(cleaned)
    except (TypeError, ValueError):
        return None
    return normalize_scalar(value)


def _mapped_span(
    source_char_ranges: tuple[tuple[int, int], ...],
    normalized_start: int,
    normalized_end: int,
    *,
    require_literal: bool,
) -> tuple[int, int] | None:
    if normalized_start < 0 or normalized_end <= normalized_start:
        return None
    if normalized_end > len(source_char_ranges):
        return None
    source_ranges = list(source_char_ranges[normalized_start:normalized_end])
    if not source_ranges:
        return None
    if require_literal:
        previous_end: int | None = None
        for start, end in source_ranges:
            if end != start + 1 or (previous_end is not None and start != previous_end):
                return None
            previous_end = end
    start = min(item[0] for item in source_ranges)
    end = max(item[1] for item in source_ranges)
    return (start, end) if end > start else None


def _quantity_cues(
    proposal: TypedScalarProposal,
    shadow: StructuralComposition,
    isolated: ResearchClauseIsolation,
    source_char_ranges: tuple[tuple[int, int], ...],
) -> tuple[ResearchMappedCue, ...] | None:
    normalized = isolated.normalized_text or ""
    original = isolated.isolated_text or ""
    is_completed_rule = shadow.receipt.rule_id == _COMPLETED_QUANTITY_RULE_ID
    relation_matches = list(
        (_COMPLETION_RELATION_RE if is_completed_rule else _RELATION_RE).finditer(normalized)
    )
    value_matches = [
        match
        for match in _NUMBER_RE.finditer(normalized)
        if _normalized_number(match.group(0)) == proposal.normalized_value
    ]
    cumulative_matches = (
        list(_CUMULATIVE_MARKER_RE.finditer(normalized)) if is_completed_rule else []
    )
    target_text = shadow.receipt.target_text
    if (
        target_text is None
        or len(relation_matches) != 1
        or len(value_matches) != 1
        or (is_completed_rule and len(cumulative_matches) != 1)
    ):
        return None
    target_range = _mapped_span(
        source_char_ranges,
        shadow.receipt.target_start or 0,
        shadow.receipt.target_end or 0,
        require_literal=True,
    )
    relation_range = _mapped_span(
        source_char_ranges,
        relation_matches[0].start(),
        relation_matches[0].end(),
        require_literal=False,
    )
    value_range = _mapped_span(
        source_char_ranges,
        value_matches[0].start(),
        value_matches[0].end(),
        require_literal=False,
    )
    cumulative_range = (
        _mapped_span(
            source_char_ranges,
            cumulative_matches[0].start(),
            cumulative_matches[0].end(),
            require_literal=True,
        )
        if is_completed_rule
        else None
    )
    if (
        target_range is None
        or relation_range is None
        or value_range is None
        or (is_completed_rule and cumulative_range is None)
    ):
        return None
    original_target = original[target_range[0]:target_range[1]]
    if original_target.casefold() != target_text.casefold():
        return None
    rule = "+".join(isolated.receipt.normalization_rules) or "identity"
    offset = isolated.source_start or 0
    cues = [
        ResearchMappedCue(
            kind="relation",
            normalized_start=relation_matches[0].start(),
            normalized_end=relation_matches[0].end(),
            source_start=offset + relation_range[0],
            source_end=offset + relation_range[1],
            rule=rule,
        ),
        ResearchMappedCue(
            kind="value",
            normalized_start=value_matches[0].start(),
            normalized_end=value_matches[0].end(),
            source_start=offset + value_range[0],
            source_end=offset + value_range[1],
            rule=rule,
        ),
        ResearchMappedCue(
            kind="target",
            normalized_start=shadow.receipt.target_start or 0,
            normalized_end=shadow.receipt.target_end or 0,
            source_start=offset + target_range[0],
            source_end=offset + target_range[1],
            rule=rule,
        ),
    ]
    if is_completed_rule:
        cues.append(
            ResearchMappedCue(
                kind="cumulative",
                normalized_start=cumulative_matches[0].start(),
                normalized_end=cumulative_matches[0].end(),
                source_start=offset + cumulative_range[0],
                source_end=offset + cumulative_range[1],
                rule=rule,
            )
        )
    return tuple(cues)


def _mapped_composition(
    proposal: TypedScalarProposal,
    shadow: StructuralComposition,
    isolation: ResearchClauseIsolation,
    cues: tuple[ResearchMappedCue, ...],
    *,
    canonical_self: bool,
) -> StructuralComposition | None:
    target_cue = next((cue for cue in cues if cue.kind == "target"), None)
    if target_cue is None or shadow.identity is None:
        return None
    target = (isolation.isolated_text or "")[
        target_cue.source_start - (isolation.source_start or 0):
        target_cue.source_end - (isolation.source_start or 0)
    ]
    try:
        identity = compose_scalar_identity(
            proposal,
            relation_type=shadow.identity.relation_type,
            target_or_scope=(target, proposal.scope),
            canonical_self=canonical_self,
            derivation_kind=_RESEARCH_DERIVATION_KIND,
            derivation_version=RESEARCH_ISOLATED_ADAPTER_VERSION,
            rule_id=shadow.identity.provenance.rule_id,
        )
    except (TypeError, ValueError):
        return None
    receipt = shadow.receipt
    return StructuralComposition(
        identity=identity,
        receipt=StructuralCompositionReceipt(
            outcome="composed",
            rule_id=receipt.rule_id,
            reason_code=None,
            relation_type=receipt.relation_type,
            target_text=target,
            target_start=target_cue.source_start,
            target_end=target_cue.source_end,
            checks=(
                "source_grounded_original",
                "proposal_source_key_original",
                "relation_cue_mapped",
                "value_cue_mapped",
                "target_literal_mapped",
                "research_surface_mapped",
            ),
            composer_version=STRUCTURAL_COMPOSER_VERSION,
        ),
    )
