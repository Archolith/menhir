"""Newer mentions of a scalar View's slot value, found in user turns at recall (#564 part 2).

The perceiver extracts what the user states, not what a question presupposes ("remember when I
got pre-approved for $400,000?"). This module finds such later mentions in episode text so recall
can list them beside the View and stop it leading unqualified. Nothing here writes or folds: the
View's value is never replaced by a mention.

Matching is lexical and deliberately narrow (v1: money only). An amount counts for a slot when the
CLAUSE around it holds a non-generic stem of the slot's attribute name, so in "buying a $325,000
house, and I got pre-approved for $350,000" only the second amount is a preapproval amount.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping

from menhir.domain.recall import NewerMention
from menhir.domain.self_identity import GATE_APPROVED_HUMAN_SOURCES
from menhir.domain.temporal import parse_iso8601

MENTION_VALUE_KINDS = frozenset({"money"})
MAX_MENTIONS_PER_VIEW = 3
MAX_QUOTE_CHARS = 300

_UNIT_BY_SYMBOL = {"$": "usd", "£": "gbp", "€": "eur"}
_MULTIPLIER = {"k": 1000, "thousand": 1000, "m": 1_000_000, "million": 1_000_000}
_NUMBER = r"(?P<num>\d{1,3}(?:,\d{3})+|\d+)(?:\.(?P<dec>\d{1,2}))?"
_MULT = r"(?:\s?(?P<mult>k|m|thousand|million)\b)?"
_SYMBOL_RE = re.compile(r"(?P<sym>[$£€])\s?" + _NUMBER + _MULT, re.IGNORECASE)
_DOLLARS_RE = re.compile(r"\b" + _NUMBER + _MULT + r"\s?(?:dollars|usd)\b", re.IGNORECASE)
_SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s+")
_CLAUSE_BREAK_RE = re.compile(r"[,;:]|\s(?:and|but|while|whereas)\s", re.IGNORECASE)
_RECOLLECTION_RE = re.compile(
    r"\b(?:remember when|do you remember|do you recall|did i (?:tell|mention))\b", re.IGNORECASE)
_WORD_RE = re.compile(r"[a-z]+")
# Attribute parts that name the kind of number, not the thing it is about.
_GENERIC = frozenset({
    "amount", "value", "total", "number", "count", "status", "current", "price", "cost",
    "usd", "dollar", "dollars", "money", "sum",
})
_SUFFIXES = ("ing", "als", "al", "ed", "es", "s")


@dataclass(frozen=True)
class MentionedAmount:
    value: Decimal
    unit: str
    sentence: str
    clause: str
    mood: str


def _stem(word: str) -> str:
    for suffix in _SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            return word[: -len(suffix)]
    return word


def text_stems(text: str) -> frozenset[str]:
    """Stems of the words in ``text`` plus each adjacent pair joined ("pre approved")."""
    words = _WORD_RE.findall(re.sub(r"(?<=[a-z])-(?=[a-z])", "", text.lower()))
    joined = [a + b for a, b in zip(words, words[1:])]
    return frozenset(_stem(w) for w in [*words, *joined])


def attribute_stems(attribute: str) -> frozenset[str]:
    """Non-generic stems of an attribute name, with adjacent parts joined (pre_approval)."""
    parts = [p for p in re.split(r"[^a-z]+", str(attribute or "").lower()) if p]
    candidates = [*parts, *(a + b for a, b in zip(parts, parts[1:]))]
    return frozenset(
        _stem(c) for c in candidates if len(c) >= 4 and c not in _GENERIC and _stem(c) not in _GENERIC
    )


def _span_text(text: str, start: int, end: int, breaks: re.Pattern[str]) -> str:
    """The piece of ``text`` between the nearest ``breaks`` around [start, end)."""
    left = 0
    for m in breaks.finditer(text, 0, start):
        left = m.end()
    right_m = breaks.search(text, end)
    right = right_m.start() if right_m else len(text)
    return text[left:right]


def _mood(sentence: str) -> str:
    if sentence.rstrip().endswith("?") or _RECOLLECTION_RE.search(sentence):
        return "question"
    return "stated"


def _amount(match: re.Match[str]) -> Decimal | None:
    raw = match.group("num").replace(",", "")
    if match.group("dec"):
        raw = f"{raw}.{match.group('dec')}"
    try:
        value = Decimal(raw)
    except InvalidOperation:
        return None
    mult = (match.group("mult") or "").lower()
    return value * _MULTIPLIER[mult] if mult else value


def find_amounts(text: str) -> list[MentionedAmount]:
    """Money amounts in ``text``, each with its sentence, clause and mood."""
    text = " ".join(str(text or "").split())
    found: list[tuple[int, int, Decimal, str]] = []
    for m in _SYMBOL_RE.finditer(text):
        value = _amount(m)
        if value is not None:
            found.append((m.start(), m.end(), value, _UNIT_BY_SYMBOL[m.group("sym")]))
    taken = [(s, e) for s, e, _, _ in found]
    for m in _DOLLARS_RE.finditer(text):
        if any(s < m.end() and m.start() < e for s, e in taken):
            continue
        value = _amount(m)
        if value is not None:
            found.append((m.start(), m.end(), value, "usd"))
    out: list[MentionedAmount] = []
    for start, end, value, unit in sorted(found, key=lambda f: f[0]):
        sentence = _span_text(text, start, end, _SENTENCE_END_RE).strip()
        clause = _span_text(text, start, end, _CLAUSE_BREAK_RE)
        out.append(MentionedAmount(value=value, unit=unit, sentence=sentence, clause=clause,
                                   mood=_mood(sentence)))
    return out


def _decimal(value: Any) -> Decimal | None:
    try:
        number = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _plain(number: Decimal) -> str:
    return format(number.normalize(), "f")


def _human(source: Any) -> bool:
    return str(source or "").lower().strip() in GATE_APPROVED_HUMAN_SOURCES


def newer_mentions(
    *,
    attribute: str,
    value_kind: str,
    unit: str,
    value: Any,
    valid_at: Any,
    episodes: Iterable[Mapping[str, Any]],
) -> tuple[NewerMention, ...]:
    """Mentions of a different same-slot value in human episodes newer than the View.

    ``episodes`` are source-memory search rows (``uuid``, ``content``, ``reference_time``,
    ``source``). An unparseable View value or time, or an episode without a parseable
    ``reference_time``, never yields a mention.
    """
    if str(value_kind or "").strip().lower() not in MENTION_VALUE_KINDS:
        return ()
    view_value = _decimal(value)
    view_time = parse_iso8601(valid_at)
    wanted = attribute_stems(attribute)
    view_unit = str(unit or "").strip().lower()
    if view_value is None or view_time is None or not wanted or not view_unit:
        return ()
    seen: set[tuple[str, str]] = set()
    out: list[NewerMention] = []
    for row in episodes:
        if not _human(row.get("source")):
            continue
        said_at = row.get("reference_time")
        said_time = parse_iso8601(said_at)
        if said_time is None or said_time <= view_time:
            continue
        for amount in find_amounts(str(row.get("content") or "")):
            if amount.unit != view_unit or amount.value == view_value:
                continue
            if not (wanted & text_stems(amount.clause)):
                continue
            quote = amount.sentence[:MAX_QUOTE_CHARS]
            key = (_plain(amount.value), quote)
            if key in seen:
                continue  # the same turn ingested twice
            seen.add(key)
            out.append(NewerMention(
                episode_uuid=str(row.get("uuid") or ""), value=_plain(amount.value), quote=quote,
                said_at=str(said_at), mood=amount.mood))
    out.sort(key=lambda m: (parse_iso8601(m.said_at), m.value, m.episode_uuid))
    return tuple(out[:MAX_MENTIONS_PER_VIEW])
