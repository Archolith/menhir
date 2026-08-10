"""Opt-in research composition through mapped clause-isolation evidence.

The canonical research adapter remains unchanged.  This seam may use a normalized surface only as
an evidence-side grammar probe; the returned proposal, source key, offsets, and composed target are
always rebuilt from the original parser proposal and literal source span.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any, Mapping, Sequence

from menhir.domain.typed_assertion import normalize_scalar
from menhir.services.compositional_scalar_identity import compose_scalar_identity
from menhir.services.research_scalar_adapter import (
    ResearchScalarResult,
    adapt_research_candidate,
)
from menhir.services.research_scalar_clause_isolator import (
    ResearchClauseIsolation,
    isolate_research_scalar_clause,
)
from menhir.services.structural_scalar_composer import (
    STRUCTURAL_COMPOSER_VERSION,
    StructuralComposition,
    StructuralCompositionReceipt,
    compose_structural_scalar_identity,
)
from menhir.services.typed_scalar_rules import TypedScalarProposal

__all__ = [
    "RESEARCH_ISOLATED_ADAPTER_VERSION",
    "ResearchMappedCue",
    "ResearchIsolatedReceipt",
    "ResearchIsolatedResult",
    "adapt_isolated_research_candidate",
]


RESEARCH_ISOLATED_ADAPTER_VERSION = "research-adapter-isolated-v2"
_QUANTITY_RULE_IDS = frozenset({
    "struct.quantity.have_v1",
    "struct.quantity.completed_v1",
})
_COMPLETED_QUANTITY_RULE_ID = "struct.quantity.completed_v1"
_REASON_PROTECTED_ROLE = "research.protected_role"
_REASON_MAPPING_UNPROVABLE = "research.surface_mapping_unprovable"
_REASON_RULE_UNSUPPORTED = "research.normalized_rule_unsupported"
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


@dataclass(frozen=True, slots=True)
class ResearchIsolatedReceipt:
    """Bounded, quote-free receipt for the opt-in mapped-surface attempt."""

    integration_version: str
    outcome: str
    mode: str
    reason: str | None
    episode_uuid: str | None
    source_key: str | None
    span_start: int | None
    span_end: int | None
    composition_rule_id: str | None
    composer_version: str | None
    normalization_rules: tuple[str, ...]
    mapped_cues: tuple[ResearchMappedCue, ...]
    checks: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ResearchIsolatedResult:
    """Canonical proposal plus optional mapped-surface composition result."""

    proposal: TypedScalarProposal | None
    composition: StructuralComposition | None
    isolation: ResearchClauseIsolation | None
    receipt: ResearchIsolatedReceipt


def _receipt(
    base: ResearchScalarResult,
    *,
    outcome: str,
    mode: str,
    reason: str | None,
    proposal: TypedScalarProposal | None,
    composition: StructuralComposition | None,
    normalization_rules: tuple[str, ...] = (),
    mapped_cues: tuple[ResearchMappedCue, ...] = (),
    checks: tuple[str, ...] = (),
) -> ResearchIsolatedReceipt:
    proposal = proposal or base.proposal
    return ResearchIsolatedReceipt(
        integration_version=RESEARCH_ISOLATED_ADAPTER_VERSION,
        outcome=outcome,
        mode=mode,
        reason=reason,
        episode_uuid=proposal.episode_uuid if proposal is not None else base.receipt.episode_uuid,
        source_key=proposal.source_key if proposal is not None else base.receipt.source_key,
        span_start=proposal.span_start if proposal is not None else base.receipt.span_start,
        span_end=proposal.span_end if proposal is not None else base.receipt.span_end,
        composition_rule_id=(
            composition.receipt.rule_id if composition is not None else None
        ),
        composer_version=(
            composition.receipt.composer_version if composition is not None else None
        ),
        normalization_rules=normalization_rules,
        mapped_cues=mapped_cues,
        checks=checks,
    )


def _result(
    base: ResearchScalarResult,
    *,
    isolation: ResearchClauseIsolation | None,
    outcome: str,
    mode: str,
    reason: str | None,
    proposal: TypedScalarProposal | None = None,
    composition: StructuralComposition | None = None,
    normalization_rules: tuple[str, ...] = (),
    mapped_cues: tuple[ResearchMappedCue, ...] = (),
    checks: tuple[str, ...] = (),
) -> ResearchIsolatedResult:
    final_proposal = proposal if proposal is not None else base.proposal
    final_composition = composition
    if final_composition is None and outcome == "composed":
        final_composition = base.composition
    return ResearchIsolatedResult(
        proposal=final_proposal,
        composition=final_composition,
        isolation=isolation,
        receipt=_receipt(
            base,
            outcome=outcome,
            mode=mode,
            reason=reason,
            proposal=final_proposal,
            composition=final_composition,
            normalization_rules=normalization_rules,
            mapped_cues=mapped_cues,
            checks=checks,
        ),
    )


def _episode_source(proposal: TypedScalarProposal, episodes: Sequence[Any]) -> str | None:
    matches = [
        str(getattr(episode, "content", "") or "")
        for episode in episodes
        if str(getattr(episode, "uuid", "") or "").strip() == proposal.episode_uuid
    ]
    return matches[0] if len(matches) == 1 else None


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


def adapt_isolated_research_candidate(
    candidate: Mapping[str, Any] | Any,
    episodes: Sequence[Any],
    *,
    candidate_id: str | None = None,
    canonical_self: bool = True,
) -> ResearchIsolatedResult:
    """Opt-in adapter path that composes normalized evidence with original provenance."""
    base = adapt_research_candidate(
        candidate,
        episodes,
        candidate_id=candidate_id,
        canonical_self=canonical_self,
    )
    proposal = base.proposal
    if proposal is None:
        return _result(
            base,
            isolation=None,
            outcome="rejected",
            mode="parser_rejected",
            reason=base.receipt.parse_reason,
        )
    source_text = _episode_source(proposal, episodes)
    if source_text is None:
        return _result(
            base,
            isolation=None,
            outcome="rejected",
            mode="source_unavailable",
            reason="research.source_unavailable",
        )
    isolation = isolate_research_scalar_clause(
        source_text,
        stated_span=proposal.stated_span,
        span_start=proposal.span_start,
        span_end=proposal.span_end,
    )
    if isolation.receipt.outcome != "isolated":
        return _result(
            base,
            isolation=isolation,
            outcome="abstained",
            mode="isolation_rejected",
            reason=isolation.receipt.reason,
        )
    if isolation.receipt.protected_roles:
        return _result(
            base,
            isolation=isolation,
            outcome="abstained",
            mode="protected_role",
            reason=_REASON_PROTECTED_ROLE,
            normalization_rules=isolation.receipt.normalization_rules,
        )
    if base.composition is not None and base.composition.composed:
        return _result(
            base,
            isolation=isolation,
            outcome="composed",
            mode="canonical",
            reason=None,
            normalization_rules=isolation.receipt.normalization_rules,
            checks=("canonical_composition",),
        )
    normalized = isolation.normalized_text
    if not normalized or normalized == isolation.isolated_text:
        return _result(
            base,
            isolation=isolation,
            outcome="abstained",
            mode="canonical_abstained",
            reason=base.receipt.composition_reason,
            normalization_rules=isolation.receipt.normalization_rules,
        )
    normalized_roles = isolate_research_scalar_clause(
        normalized,
        stated_span=normalized,
        span_start=0,
        span_end=len(normalized),
    ).receipt.protected_roles
    if normalized_roles:
        return _result(
            base,
            isolation=isolation,
            outcome="abstained",
            mode="protected_role",
            reason=_REASON_PROTECTED_ROLE,
            normalization_rules=isolation.receipt.normalization_rules,
        )
    shadow_proposal = replace(
        proposal,
        stated_span=normalized,
        span_start=0,
        span_end=len(normalized),
    )
    shadow = compose_structural_scalar_identity(shadow_proposal, normalized, canonical_self=canonical_self)
    if not shadow.composed or shadow.receipt.rule_id not in _QUANTITY_RULE_IDS:
        return _result(
            base,
            isolation=isolation,
            outcome="abstained",
            mode="normalized_abstained",
            reason=(
                shadow.receipt.reason_code
                if shadow.receipt.reason_code is not None
                else _REASON_RULE_UNSUPPORTED
            ),
            normalization_rules=isolation.receipt.normalization_rules,
        )
    cues = _quantity_cues(
        proposal,
        shadow,
        isolation,
        isolation.source_char_ranges,
    )
    if cues is None:
        return _result(
            base,
            isolation=isolation,
            outcome="abstained",
            mode="mapping_unprovable",
            reason=_REASON_MAPPING_UNPROVABLE,
            normalization_rules=isolation.receipt.normalization_rules,
        )
    composition = _mapped_composition(
        proposal,
        shadow,
        isolation,
        cues,
        canonical_self=canonical_self,
    )
    if composition is None:
        return _result(
            base,
            isolation=isolation,
            outcome="abstained",
            mode="mapping_unprovable",
            reason=_REASON_MAPPING_UNPROVABLE,
            normalization_rules=isolation.receipt.normalization_rules,
        )
    return _result(
        base,
        isolation=isolation,
        outcome="composed",
        mode="normalized_mapped",
        reason=None,
        proposal=proposal,
        composition=composition,
        normalization_rules=isolation.receipt.normalization_rules,
        mapped_cues=cues,
        checks=("original_proposal", "original_source_key", "original_target_literal", "mapped_surface_cues"),
    )
