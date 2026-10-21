"""Opt-in research composition through mapped clause-isolation evidence.

The canonical research adapter remains unchanged.  This seam may use a normalized surface only as
an evidence-side grammar probe; the returned proposal, source key, offsets, and composed target are
always rebuilt from the original parser proposal and literal source span.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Mapping, Sequence

from menhir.services.research_scalar_adapter import (
    ResearchScalarResult,
    adapt_research_candidate,
)
from menhir.services.research_scalar_clause_isolator import (
    ResearchClauseIsolation,
    isolate_research_scalar_clause,
)
from menhir.services.research_scalar_isolated_adapter_cues import (
    RESEARCH_ISOLATED_ADAPTER_VERSION,
    ResearchMappedCue,
    _QUANTITY_RULE_IDS,
    _mapped_composition,
    _mapped_span,  # noqa: F401 -- re-exported; imported from this module by tests
    _quantity_cues,
)
from menhir.services.structural_scalar_composer import (
    StructuralComposition,
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


_REASON_PROTECTED_ROLE = "research.protected_role"
_REASON_MAPPING_UNPROVABLE = "research.surface_mapping_unprovable"
_REASON_RULE_UNSUPPORTED = "research.normalized_rule_unsupported"


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
