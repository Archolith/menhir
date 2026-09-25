"""Composer version, outcome, reason-code, and check constants for the structural scalar composer.

Moved verbatim from ``structural_scalar_composer`` (facade split); the facade re-exports every
public name so existing import sites keep working unchanged.
"""

STRUCTURAL_COMPOSER_VERSION = "structural-v4"

OUTCOME_COMPOSED = "composed"
OUTCOME_ABSTAINED = "abstained"

REASON_NO_GROUNDING = "struct.no_grounding"
REASON_DUPLICATE_GROUNDING = "struct.duplicate_grounding"
REASON_SUBJECT_MISMATCH = "struct.subject_mismatch"
REASON_UNBOUND_POSSESSIVE = "struct.unbound_possessive"
REASON_OPERATION_UNSUPPORTED = "struct.operation_unsupported"
REASON_UNSAFE_QUESTION = "struct.unsafe_question"
REASON_UNSAFE_HYPOTHETICAL = "struct.unsafe_hypothetical"
REASON_UNSAFE_MODAL = "struct.unsafe_modal"
REASON_UNSAFE_HEDGE = "struct.unsafe_hedge"
REASON_UNSAFE_PAST_ONLY = "struct.unsafe_past_only"
REASON_UNSAFE_ONE_OFF = "struct.unsafe_one_off"
REASON_UNSAFE_LIST = "struct.unsafe_list"
REASON_RELATION_UNKNOWN = "struct.relation_unknown"
REASON_RULE_AMBIGUOUS = "struct.rule_ambiguous"
REASON_TARGET_UNRESOLVED = "struct.target_unresolved"
REASON_CLAIM_INCOMPLETE = "struct.claim_incomplete"
REASON_CONSTRAINT_MISMATCH = "struct.constraint_mismatch"
REASON_VALUE_MISMATCH = "struct.value_mismatch"

STRUCTURAL_REASON_CODES: frozenset[str] = frozenset({
    REASON_NO_GROUNDING,
    REASON_DUPLICATE_GROUNDING,
    REASON_SUBJECT_MISMATCH,
    REASON_UNBOUND_POSSESSIVE,
    REASON_OPERATION_UNSUPPORTED,
    REASON_UNSAFE_QUESTION,
    REASON_UNSAFE_HYPOTHETICAL,
    REASON_UNSAFE_MODAL,
    REASON_UNSAFE_HEDGE,
    REASON_UNSAFE_PAST_ONLY,
    REASON_UNSAFE_ONE_OFF,
    REASON_UNSAFE_LIST,
    REASON_RELATION_UNKNOWN,
    REASON_RULE_AMBIGUOUS,
    REASON_TARGET_UNRESOLVED,
    REASON_CLAIM_INCOMPLETE,
    REASON_CONSTRAINT_MISMATCH,
    REASON_VALUE_MISMATCH,
})

CHECKS = (
    "source_grounded",
    "grounding_unique",
    "safe_sentence_context",
    "self_subject",
    "absolute_operation",
    "relation_cue_inside_claim_span",
    "target_exact_substring",
    "target_inside_claim_span",
    "target_unique",
    "value_kind_compatible",
    "source_value_matches",
    "source_value_inside_claim_span",
    "source_unit_matches",
    "source_unit_inside_claim_span",
)
