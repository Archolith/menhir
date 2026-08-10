"""Pure compositional identity sidecar for typed-scalar shadow analysis.

This module does not extract, gate, bind, persist, or fold claims.  It gives an already grounded
``TypedScalarProposal`` a stable sidecar identity so later structural composers can compare
relation + target semantics without changing the durable TypedAssertion identity contract.

The v1 builder intentionally performs no semantic inference.  Callers may provide a relation and
open-world target/scope derived by a separately tested structural rule; otherwise the proposal's
raw attribute/scope are preserved under the safe ``state`` relation.  That fallback is diagnostic,
not a claim that two different attribute spellings are equivalent.
"""

from __future__ import annotations

import re

from menhir.domain.scalar_identity import (
    COMPOSITION_VERSION,
    RELATION_TYPES,
    CompositionalScalarIdentity,
    ScalarIdentityProvenance,
)
from menhir.domain.typed_assertion import normalize_scalar
from menhir.services.typed_scalar_rules import (
    SELF_SUBJECT_DISPLAY,
    SELF_TOKENS,
    TypedScalarProposal,
)

__all__ = [
    "COMPOSITION_VERSION",
    "RELATION_TYPES",
    "CompositionalScalarIdentity",
    "ScalarIdentityProvenance",
    "compose_scalar_identity",
]

def _normalized_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def compose_scalar_identity(
    proposal: TypedScalarProposal,
    *,
    relation_type: str = "state",
    target_or_scope: tuple[str, str] | None = None,
    canonical_self: bool = False,
    derivation_kind: str = "proposal_fields",
    derivation_version: str = COMPOSITION_VERSION,
    rule_id: str = "fallback_state",
) -> CompositionalScalarIdentity:
    """Build a deterministic sidecar from an already grounded proposal.

    The caller-supplied relation/target seam is where a later small structural grammar plugs in.
    This foundation does not infer aliases or invent an effective time.  ``canonical_self`` is
    opt-in and uses the exact allowlist already used by typed-scalar binding and voting.
    ``derivation_version`` versions the rule receipt only; semantic hashes are namespaced by the
    code-owned ``COMPOSITION_VERSION`` so a caller cannot mix normalization contracts ad hoc.
    """
    subject = _normalized_text(proposal.subject_text)
    if canonical_self and subject in SELF_TOKENS:
        subject = SELF_SUBJECT_DISPLAY

    relation = _normalized_text(relation_type)
    if relation not in RELATION_TYPES:
        raise ValueError(f"relation_type {relation!r} not in {sorted(RELATION_TYPES)}")
    custom_semantics = target_or_scope is not None or relation != "state"
    if custom_semantics and (
        derivation_kind == "proposal_fields" or rule_id == "fallback_state"
    ):
        raise ValueError(
            "caller-supplied relation/target requires an explicit derivation_kind and rule_id")
    raw_target, raw_scope = target_or_scope or (proposal.attribute, proposal.scope)
    target = _normalized_text(raw_target)
    scope = _normalized_text(raw_scope)

    provenance = ScalarIdentityProvenance(
        episode_uuid=proposal.episode_uuid.strip(),
        span_start=proposal.span_start,
        span_end=proposal.span_end,
        claim_ordinal=proposal.claim_ordinal,
        source_key=proposal.source_key,
        derivation_kind=derivation_kind.strip(),
        derivation_version=derivation_version.strip(),
        rule_id=rule_id.strip(),
    )
    return CompositionalScalarIdentity(
        subject=subject,
        relation_type=relation,
        target_or_scope=(target, scope),
        value_kind=_normalized_text(proposal.value_kind),
        value=normalize_scalar(proposal.value),
        unit=_normalized_text(proposal.unit),
        operation=_normalized_text(proposal.operation),
        effective_time=proposal.when,
        provenance=provenance,
    )
