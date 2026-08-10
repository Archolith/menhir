"""Closed, absolute-only dependency rule for the offline scalar bridge.

Parser evidence remains untrusted.  The rule first asks the source/proposal bridge to validate the
transport envelope, then re-checks a deliberately tiny source grammar and dependency topology before
calling the pure scalar identity builder.  This module has no parser, runtime, persistence, graph,
LLM, or regex-composer dependency.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from menhir.domain.scalar_identity import CompositionalScalarIdentity
from menhir.domain.typed_assertion import normalize_scalar
from menhir.services.compositional_scalar_identity import compose_scalar_identity
from menhir.services.research_scalar_dependency_bridge import (
    validate_source_bound_dependency_evidence,
)
from menhir.services.typed_scalar_rules import TypedScalarProposal
from menhir.domain.scalar_dependency_evidence import (
    ScalarDependencyEvidence,
    ScalarDependencyEvidenceReceipt,
    SourceBoundSpan,
    TokenEvidence,
)

__all__ = [
    "DEPENDENCY_COMPOSER_VERSION",
    "DEPENDENCY_RULE_ID",
    "DEPENDENCY_RULE_VERSION",
    "DependencyRuleReceipt",
    "DependencyRuleResult",
    "compose_dependency_scalar_identity",
]


DEPENDENCY_RULE_VERSION = "dependency-rule-v1"
DEPENDENCY_COMPOSER_VERSION = "dependency-composer-v1"
DEPENDENCY_RULE_ID = "dependency.absolute.quantity.v1"
_DERIVATION_KIND = "research_dependency_rule"
_PREDICATES = frozenset({"have", "own", "hold", "keep", "store"})
_TARGET_RE = re.compile(r"^[a-z]+(?:-[a-z]+)*$")
_INTEGER_RE = re.compile(r"(?<![\w-])(?:\d{1,3}(?:,\d{3})+|\d+)(?![\w-])")
_GENERIC_TARGETS = frozenset({
    "thing", "things", "item", "items", "stuff", "one", "ones", "object", "objects",
    "other", "others", "additional", "extra", "extras", "remaining", "more", "another",
})
_ANAPHORIC_TARGETS = frozenset({
    "it", "this", "that", "them", "those", "these", "we", "you", "he", "she", "him", "her",
    "us", "me", "ours", "yours", "his", "hers", "theirs", "myself", "yourself", "themselves",
})
_FUNCTION_TARGETS = frozenset({
    "a", "an", "the", "my", "our", "your", "some", "any", "all", "both", "each", "every",
    "either", "neither", "none", "no", "someone", "something", "anyone", "anything",
})
_PROTECTED_TARGETS = _GENERIC_TARGETS | _ANAPHORIC_TARGETS | _FUNCTION_TARGETS | frozenset({
    "always", "sometimes", "never", "today", "tonight", "tomorrow", "yesterday", "now", "then",
    "later", "earlier", "before", "after", "days", "weeks", "months", "years", "hours", "minutes",
    "seconds", "time", "times", "history", "histories", "past", "future", "former", "formerly",
    "previous", "previously", "current", "currently", "still", "already", "yet", "can", "could",
    "may", "might", "must", "should", "would", "will", "shall", "do", "does", "did", "not", "used",
    "use", "actually", "instead", "rather", "correction", "corrected", "oops", "sorry", "but", "and",
    "or", "has", "had", "have", "own", "hold", "keep", "store", "news",
})
_UNSAFE_TARGET_SUFFIXES = ("ss", "us", "is", "ous", "ics", "lys", "wise", "ward")


@dataclass(frozen=True, slots=True)
class DependencyRuleReceipt:
    """Bounded, quote-free result metadata for the dependency rule attempt."""

    outcome: str
    reason: str
    rule_id: str
    rule_version: str
    composer_version: str
    source_hash: str
    candidate_hash: str
    evidence_sha256: str
    clause_start: int
    clause_end: int
    target_start: int | None
    target_end: int | None
    checks: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DependencyRuleResult:
    """Identity plus the source-bound bridge and rule receipts."""

    identity: CompositionalScalarIdentity | None
    bridge_receipt: ScalarDependencyEvidenceReceipt
    receipt: DependencyRuleReceipt

    @property
    def composed(self) -> bool:
        return self.identity is not None


def _receipt(
    bridge: ScalarDependencyEvidenceReceipt,
    *,
    outcome: str,
    reason: str,
    target: SourceBoundSpan | None = None,
    checks: tuple[str, ...] = (),
) -> DependencyRuleReceipt:
    return DependencyRuleReceipt(
        outcome=outcome,
        reason=reason,
        rule_id=DEPENDENCY_RULE_ID,
        rule_version=DEPENDENCY_RULE_VERSION,
        composer_version=DEPENDENCY_COMPOSER_VERSION,
        source_hash=bridge.source_hash,
        candidate_hash=bridge.candidate_hash,
        evidence_sha256=bridge.evidence_sha256,
        clause_start=bridge.clause_start,
        clause_end=bridge.clause_end,
        target_start=target.start if target is not None else None,
        target_end=target.end if target is not None else None,
        checks=checks,
    )


def _abstain(bridge: ScalarDependencyEvidenceReceipt, reason: str, *checks: str) -> DependencyRuleResult:
    return DependencyRuleResult(
        identity=None,
        bridge_receipt=bridge,
        receipt=_receipt(bridge, outcome="abstained", reason=reason, checks=checks),
    )


def _span_token(
    span: object,
    tokens: tuple[TokenEvidence, ...],
    source: str,
    *,
    name: str,
) -> tuple[TokenEvidence, str] | None:
    if not isinstance(span, SourceBoundSpan):
        return None
    if span.token_start is None or span.token_end != span.token_start + 1:
        return None
    index = span.token_start
    if index < 0 or index >= len(tokens):
        return None
    token = tokens[index]
    if token.span != span:
        return None
    literal = source[span.start:span.end]
    if not literal or span.end > len(source):
        return None
    return token, literal


def _source_number(source: str, span: SourceBoundSpan) -> tuple[str, int] | None:
    literal = source[span.start:span.end]
    match = _INTEGER_RE.fullmatch(literal)
    if match is None:
        return None
    try:
        return literal, int(literal.replace(",", ""))
    except (TypeError, ValueError):
        return None


def _source_plural_target(target_text: str, count: int) -> str | None:
    """Apply the Phase-A source-only target grammar before parser corroboration.

    This deliberately accepts open-world regular plurals without a noun lexicon. Singular,
    irregular, mass, temporal, function-word, and adverb-like targets abstain rather than letting
    parser POS labels authorize an identity.
    """

    if count == 1:
        return "count_one_unsupported"
    if not _TARGET_RE.fullmatch(target_text):
        return "target_not_literal"
    target_lower = target_text.casefold()
    if target_lower in _PROTECTED_TARGETS:
        return "target_not_specific"
    if not target_lower.endswith("s"):
        return "target_not_plural"
    if target_lower.endswith(_UNSAFE_TARGET_SUFFIXES):
        return "target_plural_suffix_invalid"
    return None


def _topology(
    evidence: ScalarDependencyEvidence,
    source: str,
    proposal: TypedScalarProposal,
) -> tuple[SourceBoundSpan, str, tuple[str, ...]] | str:
    if evidence.clause_span.start != proposal.span_start or evidence.clause_span.end != proposal.span_end:
        return "clause_span_mismatch"
    if proposal.stated_span != source[proposal.span_start:proposal.span_end]:
        return "proposal_quote_mismatch"
    if proposal.subject_text != "I":
        return "subject_not_canonical_self"
    if proposal.scope.strip() or proposal.unit.strip() or proposal.operation != "absolute":
        return "proposal_constraints_invalid"
    if proposal.value_kind != "count" or type(proposal.value) is not int or proposal.value < 0:
        return "proposal_value_invalid"
    if evidence.cues.unit is not None or evidence.cues.scope is not None:
        return "unsupported_cue"
    if evidence.cues.modifiers or evidence.markers:
        return "markers_present"

    subject_data = _span_token(evidence.cues.subject, evidence.tokens, source, name="subject")
    predicate_data = _span_token(evidence.cues.predicate, evidence.tokens, source, name="predicate")
    numeric_data = _span_token(evidence.cues.numeric_value, evidence.tokens, source, name="numeric")
    target_data = _span_token(evidence.cues.target, evidence.tokens, source, name="target")
    if None in (subject_data, predicate_data, numeric_data, target_data):
        return "cue_token_bounds_invalid"
    assert subject_data is not None and predicate_data is not None
    assert numeric_data is not None and target_data is not None
    subject_token, subject_text = subject_data
    predicate_token, predicate_text = predicate_data
    numeric_token, _numeric_text = numeric_data
    target_token, target_text = target_data

    # Source-derived value and target grammar are authoritative. Parser labels below can only
    # corroborate this already-grounded shape; they cannot turn a singular/adverb/function token
    # into a target.
    numeric = _source_number(source, evidence.cues.numeric_value)
    if numeric is None or normalize_scalar(numeric[1]) != proposal.normalized_value:
        return "numeric_value_mismatch"
    target_reason = _source_plural_target(target_text, numeric[1])
    if target_reason is not None:
        return target_reason

    if subject_text != "I" or subject_token.dependency_label.lower() != "nsubj":
        return "subject_topology_invalid"
    if subject_token.pos_tag.upper() != "PRON":
        return "subject_pos_invalid"
    if predicate_text not in _PREDICATES or predicate_token.head_index != -1:
        return "predicate_invalid"
    if predicate_token.pos_tag.upper() not in {"VERB", "AUX"}:
        return "predicate_pos_invalid"
    if predicate_token.dependency_label.lower() != "root":
        return "predicate_root_invalid"
    if numeric_token.dependency_label.lower() != "nummod" or numeric_token.head_index != target_token.token_index:
        return "numeric_topology_invalid"
    if target_token.dependency_label.lower() not in {"dobj", "obj", "attr"}:
        return "target_topology_invalid"
    if target_token.head_index != predicate_token.token_index:
        return "target_topology_invalid"
    if target_token.pos_tag.upper() not in {"NOUN", "PROPN"}:
        return "target_pos_invalid"
    if numeric_token.pos_tag.upper() != "NUM":
        return "numeric_not_num"
    spans = (evidence.cues.subject, evidence.cues.predicate, evidence.cues.numeric_value, evidence.cues.target)
    if any(span is None for span in spans):
        return "cue_token_bounds_invalid"
    subject_span, predicate_span, numeric_span, target_span = spans
    assert subject_span is not None and predicate_span is not None
    assert numeric_span is not None and target_span is not None
    if not (
        subject_span.start < predicate_span.start < numeric_span.start < target_span.start
        and subject_span.end <= predicate_span.start
        and predicate_span.end <= numeric_span.start
        and numeric_span.end <= target_span.start
    ):
        return "source_order_invalid"
    if any(
        not source[left.end:right.start].isspace()
        for left, right in (
            (subject_span, predicate_span),
            (predicate_span, numeric_span),
            (numeric_span, target_span),
        )
    ):
        return "source_spacing_invalid"
    if source[evidence.clause_span.start:subject_span.start] or source[target_span.end:evidence.clause_span.end] not in ("", "."):
        return "source_linear_grammar_invalid"
    all_numbers = list(_INTEGER_RE.finditer(source[proposal.span_start:proposal.span_end]))
    if len(all_numbers) != 1:
        return "multiple_numeric_values"
    number_match = all_numbers[0]
    if (proposal.span_start + number_match.start(), proposal.span_start + number_match.end()) != (
        evidence.cues.numeric_value.start,
        evidence.cues.numeric_value.end,
    ):
        return "numeric_span_mismatch"

    allowed_indices = {
        subject_token.token_index,
        predicate_token.token_index,
        numeric_token.token_index,
        target_token.token_index,
    }
    for token in evidence.tokens:
        if token.token_index in allowed_indices:
            continue
        literal = source[token.span.start:token.span.end]
        if token.dependency_label.lower() != "punct" or literal != "." or token.head_index != predicate_token.token_index:
            return "extra_clause_token"
    if evidence.cues.clause_root_token != predicate_token.token_index:
        return "root_topology_invalid"
    return evidence.cues.target, target_text, (
        "bridge_validated",
        "exact_clause_span",
        "canonical_self_literal",
        "absolute_unitless_count",
        "single_integer_value",
        "single_specific_target",
        "dependency_topology",
        "no_markers",
    )


def compose_dependency_scalar_identity(
    evidence: object,
    source_text: object,
    proposal: object,
    *,
    expected_candidate_hash: object,
    expected_parser_id: object,
    expected_parser_version: object,
    expected_model_hash: object,
    expected_pipeline_hash: object,
    expected_episode_uuid: object,
    expected_source_key: object,
) -> DependencyRuleResult:
    """Validate source-bound evidence and compose the closed absolute quantity family."""

    bridge = validate_source_bound_dependency_evidence(
        evidence,
        source_text,
        proposal,
        expected_candidate_hash=expected_candidate_hash,
        expected_parser_id=expected_parser_id,
        expected_parser_version=expected_parser_version,
        expected_model_hash=expected_model_hash,
        expected_pipeline_hash=expected_pipeline_hash,
        expected_episode_uuid=expected_episode_uuid,
        expected_source_key=expected_source_key,
    )
    if bridge.outcome != "validated":
        return _abstain(bridge, "bridge_validation_failed", "bridge_validation")
    if not isinstance(evidence, ScalarDependencyEvidence) or not isinstance(source_text, str):
        return _abstain(bridge, "evidence_structure_invalid")
    if not isinstance(proposal, TypedScalarProposal):
        return _abstain(bridge, "proposal_type_invalid")
    try:
        checked = _topology(evidence, source_text, proposal)
        if isinstance(checked, str):
            return _abstain(bridge, checked, "source_rule_rejected")
        target_span, target_text, checks = checked
        identity = compose_scalar_identity(
            proposal,
            relation_type="quantity",
            target_or_scope=(target_text, ""),
            canonical_self=True,
            derivation_kind=_DERIVATION_KIND,
            derivation_version=DEPENDENCY_RULE_VERSION,
            rule_id=DEPENDENCY_RULE_ID,
        )
    except (AttributeError, IndexError, TypeError, ValueError):
        return _abstain(bridge, "rule_structure_invalid", "source_rule_rejected")
    return DependencyRuleResult(
        identity=identity,
        bridge_receipt=bridge,
        receipt=_receipt(
            bridge,
            outcome="composed",
            reason="absolute_dependency_rule_composed",
            target=target_span,
            checks=checks + ("identity_composed",),
        ),
    )
