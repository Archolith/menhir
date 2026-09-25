"""Narrow sentence-grammar rules and per-rule validators for the structural scalar composer.

Moved verbatim from ``structural_scalar_composer`` (facade split); the facade imports ``_RULES``
and keeps ``_clock_value_or_none`` importable for the CF-92 contract test.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from menhir.domain.typed_assertion import normalize_scalar
from menhir.services.structural_scalar_composer_constants import (
    REASON_CONSTRAINT_MISMATCH,
    REASON_TARGET_UNRESOLVED,
    REASON_VALUE_MISMATCH,
)
from menhir.services.typed_scalar_rules import (
    TypedScalarProposal,
    _normalize_interval_frequency,
)

_NUMBER = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
_CLOCK = r"(?:[01]?\d|2[0-3]):[0-5]\d(?:\s*(?:am|pm))?"
_TARGET = r"[a-z][a-z0-9'-]*(?:\s+[a-z][a-z0-9'-]*){0,5}"
_END = r"\s*[.!]?\s*$"

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


def _clock_value_or_none(text: str) -> str | None:
    # Fails silently (returns None) so ``_clock_validator`` can report a reason code instead of
    # raising; mirrors ``_clock_value_or_raise`` in ``deterministic_scalar_extractor``.
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
    if _clock_value_or_none(match.group("clock")) != proposal.normalized_value:
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
