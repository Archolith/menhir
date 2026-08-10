"""Pure clause/evidence isolation for scalar research candidates.

This module is deliberately upstream of the research scalar adapter.  It does not parse a scalar,
construct a proposal, or decide identity.  It only identifies one bounded source span, applies a
small set of semantics-preserving surface normalizations, and refuses corrections or competing
numeric clauses that would require interpretation.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping

__all__ = [
    "RESEARCH_CLAUSE_ISOLATOR_VERSION",
    "ResearchClauseIsolation",
    "ResearchClauseReceipt",
    "isolate_research_scalar_candidate",
    "isolate_research_scalar_clause",
]


RESEARCH_CLAUSE_ISOLATOR_VERSION = "research-clause-isolator-v1"
MAX_CLAUSE_CHARS = 512

OUTCOME_ISOLATED = "isolated"
OUTCOME_REJECTED = "rejected"

REASON_EMPTY_SOURCE = "clause.empty_source"
REASON_INVALID_OFFSETS = "clause.invalid_offsets"
REASON_SPAN_NOT_FOUND = "clause.span_not_found"
REASON_SPAN_AMBIGUOUS = "clause.span_ambiguous"
REASON_SPAN_CLAIM_MISMATCH = "clause.span_claim_mismatch"
REASON_NO_NUMERIC_CLAUSE = "clause.no_numeric_clause"
REASON_COMPETING_NUMERIC_CLAUSES = "clause.competing_numeric_clauses"
REASON_CONJUNCTION_AMBIGUOUS = "clause.conjunction_ambiguous"
REASON_CORRECTION_UNRESOLVED = "clause.correction_unresolved"
REASON_NUMERIC_FORM_UNSUPPORTED = "clause.numeric_form_unsupported"
REASON_CLAUSE_TOO_LARGE = "clause.too_large"
REASON_CANDIDATE_INVALID = "clause.candidate_invalid"

_NUMBER_WORDS = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
}
_NUMBER_TOKEN = (
    r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?|"
    + "|".join(sorted(_NUMBER_WORDS, key=len, reverse=True))
)
_NUMBER_RE = re.compile(rf"(?<![\w-])(?P<number>{_NUMBER_TOKEN})(?![\w-])", re.IGNORECASE)
_SCALE_RE = re.compile(r"\b(?:hundred|thousand|million|billion)\b", re.IGNORECASE)
_DATE_PREFIX_RE = re.compile(r"^\s*\[\d{4}-\d{2}-\d{2}\]\s*")
_LEADING_FILLER_RE = re.compile(r"^\s*(?:um|uh|erm)\b(?:\s*,\s*|\s+)", re.IGNORECASE)
_LEXICAL_POSSESSION_GOT_RE = re.compile(
    r"\b(?:I['’]ve|I\s+have)\s+got(?=\s+(?:\d|zero|one|two|three|four|five|six|seven|eight|nine|ten|"
    r"eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty)\b)",
    re.IGNORECASE,
)
_CORRECTION_RE = re.compile(
    rf"(?P<old>{_NUMBER_TOKEN})\s*(?:[—–-]\s*|,?\s+)"
    rf"(?:no|not|rather|actually)\b\s*,?\s*(?P<new>{_NUMBER_TOKEN})",
    re.IGNORECASE,
)
_CORRECTION_WORD_RE = re.compile(
    r"\b(?:actually|correction|rather|not|no)\b", re.IGNORECASE)
_CONJUNCTION_RE = re.compile(r"\b(?:and|or|but|while|then|also)\b", re.IGNORECASE)
_PUNCTUATION = ".!?;\n"

_ROLE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "negation",
        re.compile(
            r"\b(?:no|not|never|without|don't|doesn't|didn't|isn't|aren't|can't|cannot|"
            r"don['’]t|doesn['’]t|didn['’]t|isn['’]t|aren['’]t|can['’]t|won['’]t)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "modality",
        re.compile(
            r"\b(?:maybe|possibly|probably|might|may|will|would|could|should|can|want|hope|plan|"
            r"expect|going\s+to)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "history",
        re.compile(
            r"\b(?:used\s+to|previously|formerly|yesterday|ago|earlier|had|last\s+\w+)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "event",
        re.compile(
            r"\b(?:bought|purchased|ordered|spent|paid|received|earned|sold|acquired|won)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "subset",
        re.compile(
            r"\b(?:other|others|additional|extra|extras|remaining|more|another)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "temporal",
        re.compile(
            r"\b(?:now|rn|currently|today|tonight|tomorrow|later|soon|before|after|every|daily|"
            r"weekly|monthly)\b",
            re.IGNORECASE,
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class ResearchClauseReceipt:
    """Bounded, quote-free evidence about one isolation attempt."""

    isolator_version: str
    outcome: str
    reason: str | None
    source_hash: str
    source_length: int
    source_start: int | None
    source_end: int | None
    numeric_count: int
    protected_roles: tuple[str, ...]
    normalization_rules: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ResearchClauseIsolation:
    """One bounded source slice and its separately normalized research view."""

    isolated_text: str | None
    normalized_text: str | None
    source_start: int | None
    source_end: int | None
    receipt: ResearchClauseReceipt
    source_char_ranges: tuple[tuple[int, int], ...] = ()


def _source_hash(source_text: str) -> str:
    return hashlib.sha256(source_text.encode("utf-8", "replace")).hexdigest()


def _receipt(
    source_text: str,
    *,
    outcome: str,
    reason: str | None,
    source_start: int | None = None,
    source_end: int | None = None,
    numeric_count: int = 0,
    protected_roles: tuple[str, ...] = (),
    normalization_rules: tuple[str, ...] = (),
) -> ResearchClauseReceipt:
    return ResearchClauseReceipt(
        isolator_version=RESEARCH_CLAUSE_ISOLATOR_VERSION,
        outcome=outcome,
        reason=reason,
        source_hash=_source_hash(source_text),
        source_length=len(source_text),
        source_start=source_start,
        source_end=source_end,
        numeric_count=numeric_count,
        protected_roles=protected_roles,
        normalization_rules=normalization_rules,
    )


def _reject(source_text: str, reason: str, *, numeric_count: int = 0) -> ResearchClauseIsolation:
    return ResearchClauseIsolation(
        isolated_text=None,
        normalized_text=None,
        source_start=None,
        source_end=None,
        receipt=_receipt(
            source_text,
            outcome=OUTCOME_REJECTED,
            reason=reason,
            numeric_count=numeric_count,
        ),
    )


def _numeric_matches(text: str) -> list[re.Match[str]]:
    date_prefix = _DATE_PREFIX_RE.match(text)
    prefix_end = date_prefix.end() if date_prefix is not None else 0
    return [match for match in _NUMBER_RE.finditer(text) if match.start() >= prefix_end]


def _correction_present(text: str) -> bool:
    if _CORRECTION_RE.search(text) is not None:
        return True
    matches = _numeric_matches(text)
    return len(matches) >= 2 and _CORRECTION_WORD_RE.search(text) is not None


def _normalize_surface(text: str) -> tuple[str, tuple[str, ...]]:
    normalized, rules, _ranges = _normalize_surface_with_map(text)
    return normalized, rules


def _apply_transform(
    text: str,
    ranges: list[tuple[int, int]],
    pattern: re.Pattern[str],
    replacement: str | Callable[[re.Match[str]], str],
) -> tuple[str, list[tuple[int, int]], bool]:
    matches = list(pattern.finditer(text))
    if not matches:
        return text, ranges, False
    output: list[str] = []
    output_ranges: list[tuple[int, int]] = []
    cursor = 0
    changed = False
    for match in matches:
        output.append(text[cursor:match.start()])
        output_ranges.extend(ranges[cursor:match.start()])
        replacement_text = replacement(match) if callable(replacement) else replacement
        changed = changed or replacement_text != text[match.start():match.end()]
        source_start = min(start for start, _end in ranges[match.start():match.end()])
        source_end = max(end for _start, end in ranges[match.start():match.end()])
        output.append(replacement_text)
        output_ranges.extend((source_start, source_end) for _ in replacement_text)
        cursor = match.end()
    output.append(text[cursor:])
    output_ranges.extend(ranges[cursor:])
    return "".join(output), output_ranges, changed


def _normalize_surface_with_map(
    text: str,
) -> tuple[str, tuple[str, ...], tuple[tuple[int, int], ...]]:
    rules: list[str] = []
    normalized = text
    ranges = [(index, index + 1) for index in range(len(text))]
    normalized, ranges, changed = _apply_transform(
        normalized,
        ranges,
        _LEADING_FILLER_RE,
        "",
    )
    if changed:
        rules.append("leading_filler_strip")
    normalized, ranges, changed = _apply_transform(
        normalized,
        ranges,
        re.compile(r"[’‘]"),
        "'",
    )
    if changed:
        rules.append("unicode_apostrophe")
    normalized, ranges, changed = _apply_transform(
        normalized,
        ranges,
        re.compile(r"\bI['’]ve\b", re.IGNORECASE),
        "I have",
    )
    if changed:
        rules.append("contraction_ive")
    normalized, ranges, changed = _apply_transform(
        normalized,
        ranges,
        _LEXICAL_POSSESSION_GOT_RE,
        "I have",
    )
    if changed:
        rules.append("lexical_possession_got")
    normalized, ranges, changed = _apply_transform(
        normalized,
        ranges,
        re.compile(r"\bhav\b", re.IGNORECASE),
        "have",
    )
    if changed:
        rules.append("spelling_hav_have")
    normalized, ranges, changed = _apply_transform(
        normalized,
        ranges,
        re.compile(r"\b(?:have\s+){2,}", re.IGNORECASE),
        "have ",
    )
    if changed:
        rules.append("duplicate_have")

    def replace_number(match: re.Match[str]) -> str:
        value = _NUMBER_WORDS.get(match.group("number").lower())
        return str(value) if value is not None else match.group("number")

    had_number_words = any(
        re.search(rf"\b{re.escape(word)}\b", normalized, re.IGNORECASE)
        for word in _NUMBER_WORDS
    )
    normalized, ranges, changed = _apply_transform(
        normalized,
        ranges,
        _NUMBER_RE,
        replace_number,
    )
    if changed and had_number_words:
        rules.append("number_word_to_digit")
    normalized, ranges, changed = _apply_transform(
        normalized,
        ranges,
        re.compile(r"\s+"),
        " ",
    )
    if changed:
        rules.append("whitespace_collapse")
    left_trim = len(normalized) - len(normalized.lstrip())
    right_trim = len(normalized.rstrip())
    if left_trim or right_trim != len(normalized):
        normalized = normalized.strip()
        ranges = ranges[left_trim:right_trim]
        rules.append("whitespace_collapse")
    return normalized, tuple(dict.fromkeys(rules)), tuple(ranges)


def _protected_roles(text: str) -> tuple[str, ...]:
    roles = [role for role, pattern in _ROLE_PATTERNS if pattern.search(text) is not None]
    got_matches = list(re.finditer(r"\bgot\b", text, re.IGNORECASE))
    if got_matches:
        lexical_ranges = [match.span() for match in _LEXICAL_POSSESSION_GOT_RE.finditer(text)]
        if any(not any(start <= got.start() < end for start, end in lexical_ranges) for got in got_matches):
            roles.append("event")
    role_set = set(roles)
    return tuple(role for role, _pattern in _ROLE_PATTERNS if role in role_set)


def _locate_claimed_span(
    source_text: str,
    stated_span: str | None,
    span_start: int | None,
    span_end: int | None,
) -> tuple[int, int] | str:
    supplied_offsets = span_start is not None or span_end is not None
    if supplied_offsets:
        if (
            isinstance(span_start, bool)
            or not isinstance(span_start, int)
            or isinstance(span_end, bool)
            or not isinstance(span_end, int)
            or span_start < 0
            or span_end <= span_start
            or span_end > len(source_text)
        ):
            return REASON_INVALID_OFFSETS
        if stated_span is None:
            return span_start, span_end
        source_slice = source_text[span_start:span_end]
        normalized_source, _ = _normalize_surface(source_slice)
        normalized_claim, _ = _normalize_surface(stated_span)
        if normalized_source.casefold() != normalized_claim.casefold():
            return REASON_SPAN_CLAIM_MISMATCH
        return span_start, span_end
    if stated_span is None:
        return REASON_SPAN_NOT_FOUND
    claim = stated_span.strip()
    if not claim:
        return REASON_SPAN_NOT_FOUND
    exact = list(re.finditer(re.escape(claim), source_text, re.IGNORECASE))
    if len(exact) == 1:
        return exact[0].span()
    whitespace_parts = [part for part in re.split(r"\s+", claim) if part]
    whitespace_pattern = r"\s+".join(re.escape(part) for part in whitespace_parts)
    spaced = list(re.finditer(whitespace_pattern, source_text, re.IGNORECASE))
    if len(spaced) == 1:
        return spaced[0].span()
    if len(exact) > 1 or len(spaced) > 1:
        return REASON_SPAN_AMBIGUOUS
    return REASON_SPAN_NOT_FOUND


def _punctuation_segment(source_text: str, number: re.Match[str]) -> tuple[int, int]:
    left = max(source_text.rfind(mark, 0, number.start()) for mark in _PUNCTUATION)
    right_candidates = [
        index for mark in _PUNCTUATION if (index := source_text.find(mark, number.end())) >= 0
    ]
    right = min(right_candidates) + 1 if right_candidates else len(source_text)
    return left + 1, right


def isolate_research_scalar_clause(
    source_text: str,
    *,
    stated_span: str | None = None,
    span_start: int | None = None,
    span_end: int | None = None,
) -> ResearchClauseIsolation:
    """Isolate one scalar-bearing source clause without making an identity decision.

    Offsets always refer to the original ``source_text``.  ``normalized_text`` is a bounded,
    separate research view; its normalization rules are recorded in the quote-free receipt.
    Corrections and competing numeric clauses are rejected instead of selecting a number.
    """
    if not isinstance(source_text, str) or not source_text.strip():
        return _reject(str(source_text or ""), REASON_EMPTY_SOURCE)
    if len(source_text) > MAX_CLAUSE_CHARS * 8:
        return _reject(source_text, REASON_CLAUSE_TOO_LARGE)

    claimed = _locate_claimed_span(source_text, stated_span, span_start, span_end)
    if isinstance(claimed, str):
        if claimed != REASON_SPAN_NOT_FOUND or stated_span is not None or span_start is not None:
            return _reject(source_text, claimed)
        numeric_matches = _numeric_matches(source_text)
        if not numeric_matches:
            return _reject(source_text, REASON_NO_NUMERIC_CLAUSE)
        if _correction_present(source_text):
            return _reject(source_text, REASON_CORRECTION_UNRESOLVED, numeric_count=len(numeric_matches))
        if len(numeric_matches) > 1:
            return _reject(source_text, REASON_COMPETING_NUMERIC_CLAUSES, numeric_count=len(numeric_matches))
        if _CONJUNCTION_RE.search(source_text) is not None:
            return _reject(source_text, REASON_CONJUNCTION_AMBIGUOUS, numeric_count=1)
        claimed = _punctuation_segment(source_text, numeric_matches[0])

    start, end = claimed
    isolated = source_text[start:end].strip()
    leading_trim = len(source_text[start:end]) - len(source_text[start:end].lstrip())
    trailing_trim = len(source_text[start:end].rstrip())
    start += leading_trim
    end = start + (trailing_trim - leading_trim)
    if not isolated or len(isolated) > MAX_CLAUSE_CHARS:
        return _reject(source_text, REASON_CLAUSE_TOO_LARGE if isolated else REASON_NO_NUMERIC_CLAUSE)

    numeric_matches = _numeric_matches(isolated)
    if not numeric_matches:
        return _reject(source_text, REASON_NO_NUMERIC_CLAUSE)
    if _correction_present(isolated):
        return _reject(source_text, REASON_CORRECTION_UNRESOLVED, numeric_count=len(numeric_matches))
    if _SCALE_RE.search(isolated) is not None:
        return _reject(source_text, REASON_NUMERIC_FORM_UNSUPPORTED, numeric_count=len(numeric_matches))
    if len(numeric_matches) > 1:
        return _reject(source_text, REASON_COMPETING_NUMERIC_CLAUSES, numeric_count=len(numeric_matches))

    normalized, rules, source_char_ranges = _normalize_surface_with_map(isolated)
    roles = _protected_roles(isolated)
    return ResearchClauseIsolation(
        isolated_text=isolated,
        normalized_text=normalized,
        source_start=start,
        source_end=end,
        receipt=_receipt(
            source_text,
            outcome=OUTCOME_ISOLATED,
            reason=None,
            source_start=start,
            source_end=end,
            numeric_count=1,
            protected_roles=roles,
            normalization_rules=rules,
        ),
        source_char_ranges=source_char_ranges,
    )


def isolate_research_scalar_candidate(
    candidate: Mapping[str, Any],
    source_text: str,
) -> ResearchClauseIsolation:
    """Apply clause isolation to a raw research row without mutating or parsing that row."""
    if not isinstance(candidate, Mapping):
        return _reject(source_text if isinstance(source_text, str) else "", REASON_CANDIDATE_INVALID)
    claimed_span = candidate.get("stated_span")
    if claimed_span is not None and not isinstance(claimed_span, str):
        return _reject(source_text if isinstance(source_text, str) else "", REASON_CANDIDATE_INVALID)
    return isolate_research_scalar_clause(
        source_text,
        stated_span=claimed_span,
        span_start=candidate.get("span_start"),
        span_end=candidate.get("span_end"),
    )
