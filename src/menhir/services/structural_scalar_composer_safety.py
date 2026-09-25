"""Sentence-window extraction and unsafe-context/target safety checks for the structural composer.

Moved verbatim from ``structural_scalar_composer`` (facade split); the facade imports these
helpers so ``compose_structural_scalar_identity`` resolves them from its own namespace.
"""

from __future__ import annotations

import re

from menhir.services.structural_scalar_composer_constants import (
    REASON_TARGET_UNRESOLVED,
    REASON_UNSAFE_HEDGE,
    REASON_UNSAFE_HYPOTHETICAL,
    REASON_UNSAFE_LIST,
    REASON_UNSAFE_MODAL,
    REASON_UNSAFE_ONE_OFF,
    REASON_UNSAFE_PAST_ONLY,
    REASON_UNSAFE_QUESTION,
)
from menhir.services.structural_scalar_composer_rules import _QUANTITY_EMPTY_TARGETS
from menhir.services.typed_scalar_rules import TypedScalarProposal

_DATE_PREFIX_RE = re.compile(r"^\s*\[\d{4}-\d{2}-\d{2}\]\s+")
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
