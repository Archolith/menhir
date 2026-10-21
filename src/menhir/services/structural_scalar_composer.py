"""Pure structural relation/target derivation for compositional scalar shadow identity.

The composer consumes an already grounded ``TypedScalarProposal`` plus its full source episode.
It never extracts a new scalar, calls an LLM, falls back to a guessed identity, or affects runtime
decisions.  Narrow sentence grammars provide relation cues; the open target must remain a literal,
unique substring inside the proposal's own grounded span.

Constants live in ``structural_scalar_composer_constants``, the rule table and validators in
``structural_scalar_composer_rules``, and sentence-context/target safety helpers in
``structural_scalar_composer_safety``; every public name is re-exported here unchanged.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from menhir.domain.scalar_identity import CompositionalScalarIdentity
from menhir.domain.typed_assertion import validate_value
from menhir.services.compositional_scalar_identity import compose_scalar_identity
from menhir.services.structural_scalar_composer_constants import (
    CHECKS,
    OUTCOME_ABSTAINED,
    OUTCOME_COMPOSED,
    REASON_CLAIM_INCOMPLETE,
    REASON_CONSTRAINT_MISMATCH,
    REASON_DUPLICATE_GROUNDING,
    REASON_NO_GROUNDING,
    REASON_OPERATION_UNSUPPORTED,
    REASON_RELATION_UNKNOWN,
    REASON_RULE_AMBIGUOUS,
    REASON_SUBJECT_MISMATCH,
    REASON_TARGET_UNRESOLVED,
    REASON_UNBOUND_POSSESSIVE,
    REASON_UNSAFE_HEDGE,
    REASON_UNSAFE_HYPOTHETICAL,
    REASON_UNSAFE_LIST,
    REASON_UNSAFE_MODAL,
    REASON_UNSAFE_ONE_OFF,
    REASON_UNSAFE_PAST_ONLY,
    REASON_UNSAFE_QUESTION,
    REASON_VALUE_MISMATCH,
    STRUCTURAL_COMPOSER_VERSION,
    STRUCTURAL_REASON_CODES,
)
from menhir.services.structural_scalar_composer_rules import (
    _RULES,
    _clock_value_or_none as _clock_value_or_none,
)
from menhir.services.structural_scalar_composer_safety import (
    _COUNT_TEMPORAL_TAIL_RE,
    _claim_contains_consumed_cues,
    _sentence_window,
    _target_attribute_transposition_is_unsafe,
    _target_is_safe,
    _unsafe_context_reason,
)
from menhir.services.typed_scalar_rules import SELF_TOKENS, TypedScalarProposal

__all__ = [
    "CHECKS",
    "OUTCOME_ABSTAINED",
    "OUTCOME_COMPOSED",
    "REASON_CLAIM_INCOMPLETE",
    "REASON_CONSTRAINT_MISMATCH",
    "REASON_DUPLICATE_GROUNDING",
    "REASON_NO_GROUNDING",
    "REASON_OPERATION_UNSUPPORTED",
    "REASON_RELATION_UNKNOWN",
    "REASON_RULE_AMBIGUOUS",
    "REASON_SUBJECT_MISMATCH",
    "REASON_TARGET_UNRESOLVED",
    "REASON_UNBOUND_POSSESSIVE",
    "REASON_UNSAFE_HEDGE",
    "REASON_UNSAFE_HYPOTHETICAL",
    "REASON_UNSAFE_LIST",
    "REASON_UNSAFE_MODAL",
    "REASON_UNSAFE_ONE_OFF",
    "REASON_UNSAFE_PAST_ONLY",
    "REASON_UNSAFE_QUESTION",
    "REASON_VALUE_MISMATCH",
    "STRUCTURAL_COMPOSER_VERSION",
    "STRUCTURAL_REASON_CODES",
    "StructuralComposition",
    "StructuralCompositionReceipt",
    "compose_structural_scalar_identity",
]

_PLURAL_SELF_RE = re.compile(r"^\s*(?:we|our)\b", re.IGNORECASE)


@dataclass(frozen=True)
class StructuralCompositionReceipt:
    outcome: str
    rule_id: str | None
    reason_code: str | None
    relation_type: str | None
    target_text: str | None
    target_start: int | None
    target_end: int | None
    checks: tuple[str, ...]
    composer_version: str = STRUCTURAL_COMPOSER_VERSION


@dataclass(frozen=True)
class StructuralComposition:
    identity: CompositionalScalarIdentity | None
    receipt: StructuralCompositionReceipt

    @property
    def composed(self) -> bool:
        return self.identity is not None


def _abstain(reason: str, *, rule_id: str | None = None) -> StructuralComposition:
    return StructuralComposition(
        identity=None,
        receipt=StructuralCompositionReceipt(
            outcome=OUTCOME_ABSTAINED,
            rule_id=rule_id,
            reason_code=reason,
            relation_type=None,
            target_text=None,
            target_start=None,
            target_end=None,
            checks=(),
        ),
    )


def compose_structural_scalar_identity(
    proposal: TypedScalarProposal,
    source_text: str,
    *,
    canonical_self: bool = True,
) -> StructuralComposition:
    """Derive a relation + literal open target, or explicitly abstain.

    No abstention path calls ``compose_scalar_identity``: callers that want the diagnostic state
    fallback must request it separately, so structural failure can never be mistaken for success.
    """
    if not isinstance(source_text, str) or not source_text:
        return _abstain(REASON_NO_GROUNDING)
    if not isinstance(proposal.span_start, int) or not isinstance(proposal.span_end, int):
        return _abstain(REASON_NO_GROUNDING)
    if not (0 <= proposal.span_start < proposal.span_end <= len(source_text)):
        return _abstain(REASON_NO_GROUNDING)
    grounding_matches = list(re.finditer(
        re.escape(proposal.stated_span), source_text, re.IGNORECASE))
    if len(grounding_matches) > 1:
        return _abstain(REASON_DUPLICATE_GROUNDING)
    if len(grounding_matches) != 1:
        return _abstain(REASON_NO_GROUNDING)
    grounding = grounding_matches[0]
    if (grounding.start(), grounding.end()) != (proposal.span_start, proposal.span_end):
        return _abstain(REASON_NO_GROUNDING)
    try:
        validate_value(proposal.value_kind, proposal.operation, proposal.value)
    except ValueError:
        return _abstain(REASON_CONSTRAINT_MISMATCH)
    if proposal.operation != "absolute":
        return _abstain(REASON_OPERATION_UNSUPPORTED)
    if proposal.subject_text.strip().lower() not in SELF_TOKENS:
        return _abstain(REASON_SUBJECT_MISMATCH)

    sentence, sentence_start = _sentence_window(
        source_text, proposal.span_start, proposal.span_end)
    unsafe = _unsafe_context_reason(sentence)
    if unsafe is not None:
        return _abstain(unsafe)
    if _PLURAL_SELF_RE.search(sentence):
        return _abstain(REASON_SUBJECT_MISMATCH)
    if proposal.value_kind == "count" and _COUNT_TEMPORAL_TAIL_RE.search(sentence):
        return _abstain(REASON_TARGET_UNRESOLVED)
    if proposal.value_kind == "count" and re.match(r"^\s*my\b.+\bhas\b", sentence,
                                                   re.IGNORECASE):
        return _abstain(REASON_UNBOUND_POSSESSIVE)

    raw_matches = [(rule, rule.pattern.fullmatch(sentence)) for rule in _RULES]
    compatible = [
        (rule, match)
        for rule, match in raw_matches
        if match is not None and rule.value_kind == proposal.value_kind
    ]
    if len(compatible) > 1:
        return _abstain(REASON_RULE_AMBIGUOUS)
    if not compatible:
        if any(match is not None for _rule, match in raw_matches):
            return _abstain(REASON_CONSTRAINT_MISMATCH)
        return _abstain(REASON_RELATION_UNKNOWN)

    rule, match = compatible[0]
    assert match is not None
    if not _claim_contains_consumed_cues(proposal, match, sentence_start):
        return _abstain(REASON_CLAIM_INCOMPLETE, rule_id=rule.rule_id)
    target = match.group("target")
    if not _target_is_safe(target):
        return _abstain(REASON_TARGET_UNRESOLVED, rule_id=rule.rule_id)
    target_start = sentence_start + match.start("target")
    target_end = sentence_start + match.end("target")
    if not (proposal.span_start <= target_start < target_end <= proposal.span_end):
        return _abstain(REASON_TARGET_UNRESOLVED, rule_id=rule.rule_id)
    target_pattern = re.compile(
        rf"(?<![\w'-]){re.escape(target)}(?![\w'-])", re.IGNORECASE)
    if len(list(target_pattern.finditer(sentence))) != 1:
        return _abstain(REASON_TARGET_UNRESOLVED, rule_id=rule.rule_id)

    constraint_failure = rule.validator(proposal, match, sentence)
    if constraint_failure is not None:
        return _abstain(constraint_failure, rule_id=rule.rule_id)
    if _target_attribute_transposition_is_unsafe(target, proposal.attribute):
        return _abstain(REASON_TARGET_UNRESOLVED, rule_id=rule.rule_id)

    identity = compose_scalar_identity(
        proposal,
        relation_type=rule.relation_type,
        target_or_scope=(target, proposal.scope),
        canonical_self=canonical_self,
        derivation_kind="structural_grammar",
        derivation_version=STRUCTURAL_COMPOSER_VERSION,
        rule_id=rule.rule_id,
    )
    return StructuralComposition(
        identity=identity,
        receipt=StructuralCompositionReceipt(
            outcome=OUTCOME_COMPOSED,
            rule_id=rule.rule_id,
            reason_code=None,
            relation_type=rule.relation_type,
            target_text=target,
            target_start=target_start,
            target_end=target_end,
            checks=CHECKS,
        ),
    )
