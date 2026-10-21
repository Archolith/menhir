"""Cue detection and sentence analysis helpers for the deterministic scalar extractor.

Moved verbatim from `deterministic_scalar_extractor.py` (facade split): scalar-cue scanning,
sentence splitting, collision detection, and unconsumed-context checks. These serve
router-completeness reporting only — not routing itself. The facade module re-exports the
helpers the extractor calls, so every existing import site keeps working unchanged.
"""

from __future__ import annotations

import re

from menhir.services.deterministic_scalar_extractor_registry import SurfaceTemplate

#: stable abstention codes for scalar-cued text no template matched (router completeness input).
_UNMATCHED_CUE_CODES: dict[str, str] = {
    "number": "unmatched_numeric_cue",
    "currency": "unmatched_currency_cue",
    "percent": "unmatched_percent_cue",
    "clock": "unmatched_clock_cue",
    "unit": "unmatched_unit_cue",
    "freq": "unmatched_frequency_cue",
    "change": "unmatched_change_verb_cue",
    "usedto": "unmatched_used_to_cue",
    "weekday": "unmatched_weekday_cue",
    "status": "unmatched_status_cue",
    "boolean": "unmatched_boolean_cue",
    "semantic_status": "unmatched_status_cue",
    "semantic_boolean": "unmatched_boolean_cue",
    "numword": "unmatched_numeric_cue",
}

#: possessive nouns that denote SELF attributes (registry-backed; everything else is non-self).
_SELF_ATTRIBUTE_NOUNS: frozenset[str] = frozenset({
    "weight", "height", "commute", "workout", "shift", "bedtime", "wake",
    "balance", "savings", "checking", "degree", "thesis",
})

# ------------------------------------------------------------------------------------------------ #
# Cue detection and sentence analysis (router-completeness reporting only; not routing itself)
# ------------------------------------------------------------------------------------------------ #

_SCALAR_CUE_RE = re.compile(
    r"(?P<currency>[$€£])|(?P<percent>%)|(?P<clock>\d{1,2}:\d{2})|"
    r"(?P<number>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)|"
    r"(?P<unit>\b(?:kg|kilos?|kilograms?|cm|centimetres?|centimeters?|minutes?|hours?|seconds?)\b)|"
    r"(?P<freq>\b(?:once|twice|thrice|every)\b|\btimes?\s+(?:a|per)\b)|"
    r"(?P<change>\b(?:added|sold|bought|paid|spent|purchased|gave\s+away|gained|earned)\b)|"
    r"(?P<usedto>\bused\s+to\b)|"
    r"(?P<weekday>\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b)|"
    r"(?P<status>\b(?:married|employed|unemployed|retired)\b)|"
    r"(?P<boolean>\b(?:true|false)\b)|"
    # Semantic claims are cue-completeness signals only. They intentionally have no template
    # and therefore never produce deterministic authority. Exclude already-specific scalar cues
    # so existing weekday/boolean/status receipts remain stable (e.g. "my day off is Monday").
    r"(?P<semantic_status>\b(?:"
    r"(?:my|our|his|her|their)\s+[a-z][a-z0-9_-]*(?:\s+[a-z][a-z0-9_-]*)?\s+"
    r"(?:is|are|was|were)|"
    r"(?:i|we|you|they)\s+(?:am|are|is|was|were)"
    r")\s+(?!(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"true|false|married|employed|unemployed|retired)\b)[a-z][a-z0-9_-]*\b)|"
    r"(?P<semantic_boolean>\b(?:i|we|they)\s+(?:have|has|had)\s+"
    r"(?:finished|completed|read|watched|seen|visited)\b)|"
    r"(?P<numword>\b(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|dozen)\b)",
    re.IGNORECASE,
)

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9(\[])")
_PREFIX_RE = re.compile(r"^\[[^\]]*\]\s*")
_POSSESSIVE_RE = re.compile(r"\bmy\s+([a-z][a-z0-9]*)\b", re.IGNORECASE)


def _prefix_len(content: str) -> int:
    match = _PREFIX_RE.match(content)
    return match.end() if match else 0


def _split_sentences(content: str, prefix_len: int) -> list[tuple[int, int]]:
    """Split content after the synthetic date prefix into (start, end) absolute offsets.

    Boundaries come from `finditer` match positions (the whitespace separator is consumed, the
    preceding terminal punctuation stays inside the prior sentence), so absolute offsets stay
    EXACT across any number of sentences and any amount of separator whitespace.
    """
    sentences: list[tuple[int, int]] = []
    start = prefix_len
    for match in _SENTENCE_SPLIT_RE.finditer(content):
        if match.start() < prefix_len:
            continue
        sentences.append((start, match.start()))
        start = match.end()
    if start < len(content):
        sentences.append((start, len(content)))
    return sentences


def _sentence_index(position: int, sentences: list[tuple[int, int]]) -> int | None:
    for index, (start, end) in enumerate(sentences):
        if start <= position < end:
            return index
    return None


def _scalar_cues(content: str, start: int, end: int) -> list[tuple[str, int, int]]:
    """(reason_code, start, end) for every scalar cue inside [start, end).

    Numeric cues match a COMPLETE numeric token (never one hit per digit), and a number
    adjacent to a currency/percent token is folded into that single semantic cue
    ("$250", "75%" are one cue each, not two).
    """
    raw: list[tuple[str, int, int]] = []
    for match in _SCALAR_CUE_RE.finditer(content, start, end):
        label = match.lastgroup or "number"
        raw.append((label, match.start(), match.end()))
    hits: list[tuple[str, int, int]] = []
    for label, cue_start, cue_end in raw:
        if label == "number":
            folded_into = any(
                _other != "number"
                and (other_end == cue_start or other_start == cue_end)
                for _other, other_start, other_end in raw
            )
            if folded_into:
                continue
        hits.append((_UNMATCHED_CUE_CODES.get(label, "unmatched_numeric_cue"),
                     cue_start, cue_end))
    return hits


def _collision_indices(items: list[tuple[int, int, SurfaceTemplate, re.Match[str]]]) -> set[int]:
    """Indices of matches that overlap any other match (strict overlap; adjacency is fine)."""
    bad: set[int] = set()
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            if items[i][1] > items[j][0]:
                bad.add(i)
                bad.add(j)
            else:
                break
    return bad


#: terminal/joining punctuation allowed OUTSIDE the matched spans of an eligible sentence.
_UNCONSUMED_ALLOWED_CHARS = frozenset(".,!;…")
#: the ONLY connector word allowed between matched spans (multi-claim forms like
#: "I have 12 books and I wake up at 7:30." and previous/current pairs joined by commas).
_CONNECTOR_WORD_RE = re.compile(r"\band\b", re.IGNORECASE)


def _has_unconsumed_context(
    content: str,
    s_start: int,
    s_end: int,
    sentence_items: list[tuple[int, int, SurfaceTemplate, re.Match[str]]],
) -> bool:
    """True when text OUTSIDE this sentence's template matches carries semantic content the
    templates did not consume ("I have 37 coins?", "Maybe I have 37 coins.", "I have 37 coins
    for now."). Only whitespace, terminal punctuation (`.`, `!`, `…`), commas/semicolons, and
    the connector "and" may remain; anything else (including `?`) means the deterministic
    parse did not cover the whole claim and the episode must not be router-eligible. General
    by construction: the residual set is a closed contract, not per-phrase."""
    segments: list[tuple[int, int]] = [(s_start, s_end)]
    for ms, me, _t, _m in sorted(sentence_items, key=lambda item: (item[0], item[1])):
        next_segments: list[tuple[int, int]] = []
        for a, b in segments:
            if me <= a or ms >= b:
                next_segments.append((a, b))
                continue
            if a < ms:
                next_segments.append((a, ms))
            if me < b:
                next_segments.append((me, b))
        segments = next_segments
    for a, b in segments:
        text = _CONNECTOR_WORD_RE.sub(" ", content[a:b]).strip()
        if not text:
            continue
        if any(ch not in _UNCONSUMED_ALLOWED_CHARS for ch in text):
            return True
    return False
