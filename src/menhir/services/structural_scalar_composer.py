"""Pure structural relation/target derivation for compositional scalar shadow identity.

The composer consumes an already grounded ``TypedScalarProposal`` plus its full source episode.
It never extracts a new scalar, calls an LLM, falls back to a guessed identity, or affects runtime
decisions.  Narrow sentence grammars provide relation cues; the open target must remain a literal,
unique substring inside the proposal's own grounded span.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from menhir.domain.scalar_identity import CompositionalScalarIdentity
from menhir.domain.typed_assertion import normalize_scalar, validate_value
from menhir.services.compositional_scalar_identity import compose_scalar_identity
from menhir.services.typed_scalar_rules import (
    SELF_TOKENS,
    TypedScalarProposal,
    _normalize_interval_frequency,
)

__all__ = [
    "STRUCTURAL_COMPOSER_VERSION",
    "STRUCTURAL_REASON_CODES",
    "StructuralComposition",
    "StructuralCompositionReceipt",
    "compose_structural_scalar_identity",
]

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

_NUMBER = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
_CLOCK = r"(?:[01]?\d|2[0-3]):[0-5]\d(?:\s*(?:am|pm))?"
_TARGET = r"[a-z][a-z0-9'-]*(?:\s+[a-z][a-z0-9'-]*){0,5}"
_END = r"\s*[.!]?\s*$"

_DATE_PREFIX_RE = re.compile(r"^\s*\[\d{4}-\d{2}-\d{2}\]\s+")
_PLURAL_SELF_RE = re.compile(r"^\s*(?:we|our)\b", re.IGNORECASE)
_QUESTION_START_RE = re.compile(r"^\s*(?:did|do|does|is|are|can|could|would)\b", re.IGNORECASE)
_HYPOTHETICAL_RE = re.compile(r"\b(?:if|unless|would|could)\b", re.IGNORECASE)
_MODAL_RE = re.compile(
    r"\b(?:maybe|might|may|will|hope|hoping|plan|planning|want|expect|tomorrow|going\s+to|"
    r"next\s+(?:day|week|month|year))\b",
    re.IGNORECASE,
)
_HEDGE_RE = re.compile(
    r"\b(?:about|around|roughly|approximately|more\s+than|less\s+than|at\s+least|"
    r"at\s+most|over|under|usually|sometimes|often|generally|typically)\b|\d\s+or\s+\d",
    re.IGNORECASE,
)
_PAST_ONLY_RE = re.compile(
    r"\b(?:used\s+to|previously|formerly|yesterday|ago|a\s+while\s+ago|"
    r"last\s+(?:night|day|week|month|year))\b",
    re.IGNORECASE,
)
_TEMPORAL_TAIL_RE = re.compile(
    r"\b(?:as\s+of|since|starting|until|through|next|previous|future|upcoming)\b",
    re.IGNORECASE,
)
_COUNT_TEMPORAL_TAIL_RE = re.compile(
    r"\b(?:next|this|before|after|later|soon|tonight|every|daily|weekly|monthly|yearly|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|at|by)\b",
    re.IGNORECASE,
)
_ONE_OFF_RE = re.compile(
    r"\b(?:paid|spent|bought|purchased|ordered|met|meet|attended|appointment|flight)\b",
    re.IGNORECASE,
)
_LIST_RE = re.compile(r"[;,]|\b(?:and|or)\b", re.IGNORECASE)
_ANAPHORIC_TARGETS = frozenset({"it", "this", "that", "them", "those", "these"})
_FUNCTION_WORDS = frozenset({"a", "an", "the", "my", "our", "to", "of", "in", "on", "for"})
_QUANTITY_UNSAFE_TARGET_TOKENS = frozenset({
    "in", "on", "for", "to", "from", "with", "now", "currently", "today", "as", "of",
    "since", "starting", "until", "through", "ago", "last",
    # These modifiers describe an unbound subset rather than a stable target noun.
    "other", "others", "additional", "extra", "extras", "remaining", "more", "another",
})
_QUANTITY_EMPTY_TARGETS = frozenset({
    # Generic placeholders do not identify what is being counted.
    "thing", "things", "item", "items", "stuff", "one", "ones",
})

_UNIT_CANON = {
    "$": "usd",
    "usd": "usd",
    "dollar": "usd",
    "dollars": "usd",
    "kg": "kg",
    "kilo": "kg",
    "kilos": "kg",
    "kilogram": "kg",
    "kilograms": "kg",
    "cm": "cm",
    "centimeter": "cm",
    "centimeters": "cm",
    "centimetre": "cm",
    "centimetres": "cm",
    "minute": "minutes",
    "minutes": "minutes",
    "hour": "hours",
    "hours": "hours",
    "day": "day",
    "days": "day",
    "week": "week",
    "weeks": "week",
    "month": "month",
    "months": "month",
    "year": "year",
    "years": "year",
    "%": "percent",
    "percent": "percent",
}


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


@dataclass(frozen=True)
class _Rule:
    rule_id: str
    relation_type: str
    value_kind: str
    pattern: re.Pattern[str]
    validator: Callable[[TypedScalarProposal, re.Match[str], str], str | None]


def _number_value(text: str) -> str:
    cleaned = text.replace(",", "")
    value: int | float = float(cleaned) if "." in cleaned else int(cleaned)
    return normalize_scalar(value)


def _canonical_source_unit(text: str | None) -> str | None:
    return _UNIT_CANON.get(str(text or "").strip().lower())


def _numeric_unit_validator(
    proposal: TypedScalarProposal,
    match: re.Match[str],
    allowed_units: frozenset[str],
) -> str | None:
    if _number_value(match.group("number")) != proposal.normalized_value:
        return REASON_VALUE_MISMATCH
    source_unit = _canonical_source_unit(match.groupdict().get("unit"))
    if (
        not source_unit
        or source_unit not in allowed_units
        or source_unit != proposal.unit.strip().lower()
    ):
        return REASON_CONSTRAINT_MISMATCH
    return None


def _measurement_validator(
    proposal: TypedScalarProposal,
    match: re.Match[str],
    _sentence: str,
) -> str | None:
    return _numeric_unit_validator(proposal, match, frozenset({"kg", "cm", "percent"}))


def _duration_validator(
    proposal: TypedScalarProposal,
    match: re.Match[str],
    _sentence: str,
) -> str | None:
    return _numeric_unit_validator(proposal, match, frozenset({"minutes", "hours"}))


def _frequency_rate_validator(
    proposal: TypedScalarProposal,
    match: re.Match[str],
    _sentence: str,
) -> str | None:
    return _numeric_unit_validator(proposal, match, frozenset({"day", "week", "month", "year"}))


def _quantity_validator(
    proposal: TypedScalarProposal,
    match: re.Match[str],
    _sentence: str,
) -> str | None:
    if proposal.unit.strip():
        return REASON_CONSTRAINT_MISMATCH
    if _number_value(match.group("number")) != proposal.normalized_value:
        return REASON_VALUE_MISMATCH
    target_tokens = set(match.group("target").lower().split())
    normalized_target = match.group("target").strip().lower()
    if (
        target_tokens & _QUANTITY_UNSAFE_TARGET_TOKENS
        or normalized_target in _QUANTITY_EMPTY_TARGETS
    ):
        return REASON_TARGET_UNRESOLVED
    return None


def _completed_quantity_validator(
    proposal: TypedScalarProposal,
    match: re.Match[str],
    sentence: str,
) -> str | None:
    """Validate a present-perfect cumulative count without widening quantity semantics."""
    failure = _quantity_validator(proposal, match, sentence)
    if failure is not None:
        return failure
    target_tokens = set(match.group("target").lower().split())
    if target_tokens & frozenset({"total", "current", "previous", "prior"}):
        return REASON_TARGET_UNRESOLVED
    return None


def _money_validator(
    proposal: TypedScalarProposal,
    match: re.Match[str],
    _sentence: str,
) -> str | None:
    if _number_value(match.group("number")) != proposal.normalized_value:
        return REASON_VALUE_MISMATCH
    groups = match.groupdict()
    source_unit = "$" if groups.get("currency") else groups.get("unit")
    if not source_unit or _canonical_source_unit(source_unit) != proposal.unit.strip().lower():
        return REASON_CONSTRAINT_MISMATCH
    return None


def _clock_value(text: str) -> str | None:
    match = re.fullmatch(r"(?P<hour>[01]?\d|2[0-3]):(?P<minute>[0-5]\d)\s*(?P<meridiem>am|pm)?", text,
                         re.IGNORECASE)
    if match is None:
        return None
    hour = int(match.group("hour"))
    meridiem = (match.group("meridiem") or "").lower()
    if meridiem:
        if not 1 <= hour <= 12:
            return None
        if meridiem == "pm" and hour < 12:
            hour += 12
        elif meridiem == "am" and hour == 12:
            hour = 0
    return f"{hour:02d}:{match.group('minute')}"


def _clock_validator(
    proposal: TypedScalarProposal,
    match: re.Match[str],
    _sentence: str,
) -> str | None:
    if proposal.unit.strip():
        return REASON_CONSTRAINT_MISMATCH
    if _clock_value(match.group("clock")) != proposal.normalized_value:
        return REASON_VALUE_MISMATCH
    return None


def _frequency_interval_validator(
    proposal: TypedScalarProposal,
    _match: re.Match[str],
    sentence: str,
) -> str | None:
    normalized = _normalize_interval_frequency(sentence)
    if normalized is None:
        return REASON_VALUE_MISMATCH
    value, unit = normalized
    if normalize_scalar(value) != proposal.normalized_value:
        return REASON_VALUE_MISMATCH
    if unit != proposal.unit.strip().lower():
        return REASON_CONSTRAINT_MISMATCH
    return None


_RULES: tuple[_Rule, ...] = (
    _Rule(
        "struct.balance.possessive_v1",
        "balance",
        "money",
        re.compile(
            rf"^my\s+(?P<target>{_TARGET})\s+(?P<relation>balance)\s+is\s+"
            rf"(?P<currency>\$)?(?P<number>{_NUMBER})(?:\s*(?P<unit>usd|dollars?))?{_END}",
            re.IGNORECASE,
        ),
        _money_validator,
    ),
    _Rule(
        "struct.balance.account_v1",
        "balance",
        "money",
        re.compile(
            rf"^i\s+have\s+(?P<currency>\$)?(?P<number>{_NUMBER})"
            rf"(?:\s*(?P<unit>usd|dollars?))?\s+in\s+my\s+"
            rf"(?P<target>{_TARGET})\s+(?P<relation>account){_END}",
            re.IGNORECASE,
        ),
        _money_validator,
    ),
    _Rule(
        "struct.frequency.rate_v1",
        "frequency",
        "frequency",
        re.compile(
            rf"^i\s+(?P<target>{_TARGET})\s+(?P<number>{_NUMBER})\s+"
            rf"(?P<relation>times?)\s+"
            rf"(?:a|per)\s+(?P<unit>days?|weeks?|months?|years?){_END}",
            re.IGNORECASE,
        ),
        _frequency_rate_validator,
    ),
    _Rule(
        "struct.frequency.interval_v1",
        "frequency",
        "frequency",
        re.compile(
            rf"^i\s+(?P<target>{_TARGET})\s+(?P<relation>every)\s+"
            rf"(?:(?P<interval>other|{_NUMBER})\s+)?"
            rf"(?P<unit>days?|weeks?|months?|years?){_END}",
            re.IGNORECASE,
        ),
        _frequency_interval_validator,
    ),
    _Rule(
        "struct.schedule_time.possessive_v1",
        "schedule_time",
        "clock_time",
        re.compile(
            rf"^my\s+(?P<target>wake\s+time|bed\s+time|bedtime)\s+"
            rf"(?P<relation>is)\s+"
            rf"(?P<clock>{_CLOCK}){_END}",
            re.IGNORECASE,
        ),
        _clock_validator,
    ),
    _Rule(
        "struct.schedule_time.self_v1",
        "schedule_time",
        "clock_time",
        re.compile(
            rf"^i\s+(?P<target>wake(?:\s+up)?|go\s+to\s+bed)\s+"
            rf"(?P<relation>at)\s+(?P<clock>{_CLOCK}){_END}",
            re.IGNORECASE,
        ),
        _clock_validator,
    ),
    _Rule(
        "struct.duration.self_recurring_v1",
        "duration",
        "duration",
        re.compile(
            rf"^i\s+(?P<target>sleep)\s+(?P<number>{_NUMBER})\s+"
            rf"(?P<unit>hours?)\s+(?P<relation>a|per)\s+(?P<period>night){_END}",
            re.IGNORECASE,
        ),
        _duration_validator,
    ),
    _Rule(
        "struct.measurement.possessive_weight_v1",
        "measurement",
        "measurement",
        re.compile(
            rf"^my\s+(?P<target>weight)\s+(?P<relation>is)\s+"
            rf"(?P<number>{_NUMBER})\s+"
            rf"(?P<unit>kg|kilos?|kilograms?){_END}",
            re.IGNORECASE,
        ),
        _measurement_validator,
    ),
    _Rule(
        "struct.measurement.possessive_height_v1",
        "measurement",
        "measurement",
        re.compile(
            rf"^my\s+(?P<target>height)\s+(?P<relation>is)\s+"
            rf"(?P<number>{_NUMBER})\s+"
            rf"(?P<unit>cm|centimeters?|centimetres?){_END}",
            re.IGNORECASE,
        ),
        _measurement_validator,
    ),
    _Rule(
        "struct.measurement.self_tall_v1",
        "measurement",
        "measurement",
        re.compile(
            rf"^i\s+(?P<relation>am)\s+(?P<number>{_NUMBER})\s+"
            rf"(?P<unit>cm|centimeters?|centimetres?)\s+(?P<target>tall){_END}",
            re.IGNORECASE,
        ),
        _measurement_validator,
    ),
    _Rule(
        "struct.measurement.self_v1",
        "measurement",
        "measurement",
        re.compile(
            rf"^i\s+(?P<target>(?P<relation>weigh))\s+"
            rf"(?P<number>{_NUMBER})\s+"
            rf"(?P<unit>kg|kilos?|kilograms?){_END}",
            re.IGNORECASE,
        ),
        _measurement_validator,
    ),
    _Rule(
        "struct.quantity.have_v1",
        "quantity",
        "count",
        re.compile(
            rf"^i\s+(?P<relation>have|hold|own|possess)\s+"
            rf"(?P<number>{_NUMBER})\s+"
            rf"(?P<target>{_TARGET}){_END}",
            re.IGNORECASE,
        ),
        _quantity_validator,
    ),
    _Rule(
        "struct.quantity.completed_v1",
        "quantity",
        "count",
        re.compile(
            rf"^i\s+have\s+(?P<relation>completed|finished|closed)\s+"
            rf"(?P<number>{_NUMBER})\s+(?P<target>{_TARGET})\s+"
            rf"(?P<cumulative>so\s+far|to\s+date){_END}",
            re.IGNORECASE,
        ),
        _completed_quantity_validator,
    ),
)


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


def _sentence_window(source_text: str, span_start: int, span_end: int) -> tuple[str, int]:
    left = max(source_text.rfind(mark, 0, span_start) for mark in ".!?;\n")
    if source_text[span_end - 1] in ".!?;\n":
        right = span_end
    else:
        right_candidates = [
            index
            for mark in ".!?;\n"
            if (index := source_text.find(mark, span_end)) >= 0
        ]
        right = min(right_candidates) + 1 if right_candidates else len(source_text)
    start = left + 1
    while start < right and source_text[start].isspace():
        start += 1
    window = source_text[start:right]
    date_prefix = _DATE_PREFIX_RE.match(window)
    if date_prefix is not None:
        start += date_prefix.end()
        window = source_text[start:right]
    return window, start


def _unsafe_context_reason(sentence: str) -> str | None:
    without_numeric_commas = re.sub(r"(?<=\d),(?=\d)", "", sentence)
    if "?" in sentence or _QUESTION_START_RE.search(sentence):
        return REASON_UNSAFE_QUESTION
    if _HYPOTHETICAL_RE.search(sentence):
        return REASON_UNSAFE_HYPOTHETICAL
    if _MODAL_RE.search(sentence):
        return REASON_UNSAFE_MODAL
    if _HEDGE_RE.search(sentence):
        return REASON_UNSAFE_HEDGE
    if _PAST_ONLY_RE.search(sentence):
        return REASON_UNSAFE_PAST_ONLY
    if _TEMPORAL_TAIL_RE.search(sentence):
        return REASON_TARGET_UNRESOLVED
    if _ONE_OFF_RE.search(sentence):
        return REASON_UNSAFE_ONE_OFF
    if _LIST_RE.search(without_numeric_commas):
        return REASON_UNSAFE_LIST
    return None


def _target_is_safe(target: str) -> bool:
    normalized = target.strip().lower()
    tokens = normalized.split()
    return bool(
        normalized
        and not any(char.isdigit() for char in normalized)
        and normalized not in _ANAPHORIC_TARGETS
        and any(token not in _FUNCTION_WORDS for token in tokens)
    )


def _target_attribute_transposition_is_unsafe(
    target: str,
    attribute: str,
) -> bool:
    """Reject a likely adjacent-transposition typo without correcting its identity.

    This is intentionally edit-local: both values must be one alphabetic token of equal length,
    with exactly two adjacent mismatches that swap into one another. Generic proposal attributes
    remain non-authoritative, and no dictionary or fuzzy correction is involved.
    """
    source_token = target.strip().casefold()
    attribute_token = attribute.strip().casefold()
    if (
        not source_token
        or not attribute_token
        or source_token in _QUANTITY_EMPTY_TARGETS
        or attribute_token in _QUANTITY_EMPTY_TARGETS
        or not source_token.isalpha()
        or not attribute_token.isalpha()
        or len(source_token) != len(attribute_token)
    ):
        return False
    mismatches = [
        index
        for index, (source_char, attribute_char) in enumerate(zip(source_token, attribute_token))
        if source_char != attribute_char
    ]
    if len(mismatches) != 2 or mismatches[1] != mismatches[0] + 1:
        return False
    index = mismatches[0]
    swapped = list(source_token)
    swapped[index], swapped[index + 1] = swapped[index + 1], swapped[index]
    return "".join(swapped) == attribute_token


def _claim_contains_consumed_cues(
    proposal: TypedScalarProposal,
    match: re.Match[str],
    sentence_start: int,
) -> bool:
    required_groups = ["target", "relation"]
    groups = match.groupdict()
    if groups.get("clock") is not None:
        required_groups.append("clock")
    elif groups.get("number") is not None:
        required_groups.append("number")
    elif groups.get("interval") is not None:
        required_groups.append("interval")
    if groups.get("period") is not None:
        required_groups.append("period")
    if groups.get("cumulative") is not None:
        required_groups.append("cumulative")
    if proposal.unit.strip():
        present_unit_groups = [
            group
            for group in ("currency", "unit")
            if groups.get(group) is not None
        ]
        if not present_unit_groups:
            return False
        required_groups.extend(present_unit_groups)
    for group in required_groups:
        if group not in match.groupdict() or match.start(group) < 0:
            return False
        start = sentence_start + match.start(group)
        end = sentence_start + match.end(group)
        if not (proposal.span_start <= start < end <= proposal.span_end):
            return False
    return True


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
