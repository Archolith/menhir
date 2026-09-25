"""Closed, versioned surface-template registry for the deterministic scalar extractor.

Moved verbatim from `deterministic_scalar_extractor.py` (facade split): the literal surface
patterns, their capture/derivation helpers, the `SurfaceTemplate` dataclass, and the closed
`TEMPLATE_REGISTRY`. The facade module re-exports `SurfaceTemplate` and `TEMPLATE_REGISTRY`,
so every existing import site keeps working unchanged.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

from menhir.services.typed_scalar_rules import SELF_SUBJECT_DISPLAY

# ------------------------------------------------------------------------------------------------ #
# Surface template registry (CLOSED, versioned). Every entry maps one literal surface pattern to
# exactly one canonical snake_case slot; unknown surfaces abstain by construction.
# ------------------------------------------------------------------------------------------------ #

_NUM_TOKEN = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"
_NOUNS = r"coins|books|cats|dogs|records|plants|stamps"
_NOUN_GROUP = rf"(?P<noun>{_NOUNS})"
_MASS_UNIT = r"kg|kilos?|kilograms?"
_HEIGHT_UNIT = r"cm|centimetres?|centimeters?"
_CLOCK = r"(?P<hour>[01]?\d|2[0-3]):(?P<minute>[0-5]\d)\s*(?P<meridiem>am|pm)?"
_HAVE_PHRASE = r"i(?:'ve\s+got|'ve|\s+have(?:\s+got)?)"


def _parse_number(text: str | None) -> int | float:
    """Strict numeric parse of a template-captured number (commas allowed; int stays int)."""
    cleaned = (text or "").replace(",", "")
    if "." not in cleaned:
        return int(cleaned)
    return float(cleaned)


def _clock_value_or_raise(match: re.Match[str]) -> str:
    """Normalize a captured clock time to zero-padded HH:MM.

    A 12h AM/PM suffix is only valid on hours 1..12; mixed 24h+meridiem forms ("22:15 PM",
    "00:15 am") raise ValueError so the candidate drops as a template mismatch and the episode
    is never router-eligible. 24h-without-meridiem and 12h forms stay distinct and valid.

    The ``_or_raise`` suffix is load-bearing (CF-92): ``structural_scalar_composer`` applies the
    identical rule and returns ``None`` instead, because its caller reports a reason code. The two
    were both named ``_clock_value``, which read as interchangeable and was not.
    """
    hour = int(match.group("hour"))
    minute = match.group("minute")
    meridiem = (match.group("meridiem") or "").lower()
    if meridiem:
        if not 1 <= hour <= 12:
            raise ValueError(f"meridiem hour out of range: {hour}")
        if meridiem == "pm" and hour < 12:
            hour += 12
        elif meridiem == "am" and hour == 12:
            hour = 0
    return f"{hour:02d}:{minute}"


_MASS_UNIT_TO_CANON = {"kg": "kg", "kilo": "kg", "kilos": "kg",
                       "kilogram": "kg", "kilograms": "kg"}
_HEIGHT_UNIT_TO_CANON = {"cm": "cm", "centimeter": "cm", "centimeters": "cm",
                         "centimetre": "cm", "centimetres": "cm"}
_DURATION_UNIT_TO_CANON = {"minute": "minutes", "minutes": "minutes",
                           "hour": "hours", "hours": "hours"}
_PERIOD_TO_CANON = {"day": "day", "days": "day", "week": "week", "weeks": "week",
                    "month": "month", "months": "month", "year": "year", "years": "year"}
_ACCUMULATOR_PHRASE = r"(?:collection|inventory|holdings)"
_ACTIVITY_TO_SLOT = {"go to the gym": "gym_frequency", "run": "run_frequency",
                     "swim": "swim_frequency"}


def _unit_from(mapping: dict[str, str]) -> Callable[[re.Match[str]], str]:
    return lambda m: mapping.get((m.group("unit") or "").lower(), m.group("unit"))


def _interval_placeholder_value(match: re.Match[str]) -> int:
    """Placeholder for 'every ...' frequency forms; `parse_scalar_row` overwrites it via the
    shared interval normalizer, which is verified before admission (see extract)."""
    return 1


@dataclass(frozen=True)
class SurfaceTemplate:
    """ONE closed surface -> canonical slot mapping. `pattern` must capture the number in a
    named group `n` (or `lo`/`hi` for ranges, `hour`/`minute`/`meridiem` for clock times);
    `value_from`/`attribute_from`/`unit_from` override the default derivations."""

    template_id: str
    class_id: str
    attribute: str
    value_kind: str
    unit: str
    operation: str
    pattern: re.Pattern[str]
    subject: str = SELF_SUBJECT_DISPLAY
    scope: str = ""
    value_from: Callable[[re.Match[str]], Any] | None = field(default=None, compare=False)
    attribute_from: Callable[[re.Match[str]], str] | None = field(default=None, compare=False)
    unit_from: Callable[[re.Match[str]], str] | None = field(default=None, compare=False)


TEMPLATE_REGISTRY: tuple[SurfaceTemplate, ...] = (
    # ---- c_count: explicit self-held tallies -----------------------------------------------
    SurfaceTemplate("count_have", "c_count", "coins", "count", "", "absolute",
                    re.compile(rf"(?<!now\s)\b{_HAVE_PHRASE}\s+(?P<n>{_NUM_TOKEN})\s+{_NOUN_GROUP}\b",
                               re.IGNORECASE), attribute_from=lambda m: m.group("noun")),
    SurfaceTemplate("count_now_have", "c_count", "coins", "count", "", "absolute",
                    re.compile(rf"\bnow\s+{_HAVE_PHRASE}\s+(?P<n>{_NUM_TOKEN})\s+{_NOUN_GROUP}\b",
                               re.IGNORECASE), attribute_from=lambda m: m.group("noun")),
    SurfaceTemplate("count_have_now", "c_count", "coins", "count", "", "absolute",
                    re.compile(rf"(?<!now\s)\bi\s+now\s+(?:have|'ve\s+got)\s+(?P<n>{_NUM_TOKEN})\s+{_NOUN_GROUP}\b",
                               re.IGNORECASE), attribute_from=lambda m: m.group("noun")),
    # ---- c_range: closed numeric interval ---------------------------------------------------
    SurfaceTemplate("count_range", "c_range", "coins", "count", "", "absolute",
                    re.compile(rf"\bi\s+have\s+between\s+(?P<lo>{_NUM_TOKEN})\s+and\s+(?P<hi>{_NUM_TOKEN})\s+{_NOUN_GROUP}\b",
                               re.IGNORECASE),
                    attribute_from=lambda m: m.group("noun"),
                    value_from=lambda m: [_parse_number(m.group("lo")),
                                          _parse_number(m.group("hi"))]),
    SurfaceTemplate("measure_weight_range", "c_range", "weight", "measurement", "kg", "absolute",
                    re.compile(rf"\bi\s+weigh\s+between\s+(?P<lo>{_NUM_TOKEN})\s+and\s+(?P<hi>{_NUM_TOKEN})\s+(?P<unit>{_MASS_UNIT})\b",
                               re.IGNORECASE),
                    unit_from=_unit_from(_MASS_UNIT_TO_CANON),
                    value_from=lambda m: [_parse_number(m.group("lo")),
                                          _parse_number(m.group("hi"))]),
    SurfaceTemplate("measure_height_range", "c_range", "height", "measurement", "cm", "absolute",
                    re.compile(rf"\bmy\s+height\s+is\s+between\s+(?P<lo>{_NUM_TOKEN})\s+and\s+(?P<hi>{_NUM_TOKEN})\s+(?P<unit>{_HEIGHT_UNIT})\b",
                               re.IGNORECASE),
                    unit_from=_unit_from(_HEIGHT_UNIT_TO_CANON),
                    value_from=lambda m: [_parse_number(m.group("lo")),
                                          _parse_number(m.group("hi"))]),
    # ---- c_money: explicit self balances ----------------------------------------------------
    SurfaceTemplate("money_balance_dollar", "c_money", "savings_balance", "money", "usd", "absolute",
                    re.compile(rf"\bmy\s+(?P<account>savings|checking)\s+balance\s+is\s+\$(?P<n>{_NUM_TOKEN})\b",
                               re.IGNORECASE),
                    attribute_from=lambda m: f"{m.group('account').lower()}_balance"),
    SurfaceTemplate("money_balance_dollars", "c_money", "savings_balance", "money", "usd", "absolute",
                    re.compile(rf"\bmy\s+(?P<account>savings|checking)\s+balance\s+is\s+(?P<n>{_NUM_TOKEN})\s+dollars?\b",
                               re.IGNORECASE),
                    attribute_from=lambda m: f"{m.group('account').lower()}_balance"),
    SurfaceTemplate("money_account_dollar", "c_money", "savings_balance", "money", "usd", "absolute",
                    re.compile(rf"\bi\s+have\s+\$(?P<n>{_NUM_TOKEN})\s+in\s+my\s+(?P<account>savings|checking)\s+account\b",
                               re.IGNORECASE),
                    attribute_from=lambda m: f"{m.group('account').lower()}_balance"),
    SurfaceTemplate("money_account_dollars", "c_money", "savings_balance", "money", "usd", "absolute",
                    re.compile(rf"\bi\s+have\s+(?P<n>{_NUM_TOKEN})\s+dollars?\s+in\s+my\s+(?P<account>savings|checking)\s+account\b",
                               re.IGNORECASE),
                    attribute_from=lambda m: f"{m.group('account').lower()}_balance"),
    # ---- c_measurement: self body measurements ----------------------------------------------
    SurfaceTemplate("measure_weight_i", "c_measurement", "weight", "measurement", "kg", "absolute",
                    re.compile(rf"(?<!now\s)(?<!actually,\s)\bi\s+weigh\s+(?P<n>{_NUM_TOKEN})\s+(?P<unit>{_MASS_UNIT})\b",
                               re.IGNORECASE),
                    unit_from=_unit_from(_MASS_UNIT_TO_CANON)),
    SurfaceTemplate("measure_weight_correction", "c_measurement", "weight", "measurement", "kg",
                    "absolute",
                    re.compile(rf"\bactually,\s+i\s+weigh\s+(?P<n>{_NUM_TOKEN})\s+(?P<unit>{_MASS_UNIT})\b",
                               re.IGNORECASE),
                    unit_from=_unit_from(_MASS_UNIT_TO_CANON)),
    SurfaceTemplate("measure_weight_my", "c_measurement", "weight", "measurement", "kg", "absolute",
                    re.compile(rf"\bmy\s+weight\s+is\s+(?P<n>{_NUM_TOKEN})\s+(?P<unit>{_MASS_UNIT})\b",
                               re.IGNORECASE),
                    unit_from=_unit_from(_MASS_UNIT_TO_CANON)),
    SurfaceTemplate("measure_weight_now", "c_measurement", "weight", "measurement", "kg", "absolute",
                    re.compile(rf"\bnow\s+i\s+weigh\s+(?P<n>{_NUM_TOKEN})\s+(?P<unit>{_MASS_UNIT})\b",
                               re.IGNORECASE),
                    unit_from=_unit_from(_MASS_UNIT_TO_CANON)),
    SurfaceTemplate("measure_height_i", "c_measurement", "height", "measurement", "cm", "absolute",
                    re.compile(rf"(?<!now\s)\bi\s+am\s+(?P<n>{_NUM_TOKEN})\s+(?P<unit>{_HEIGHT_UNIT})\s+tall\b",
                               re.IGNORECASE),
                    unit_from=_unit_from(_HEIGHT_UNIT_TO_CANON)),
    SurfaceTemplate("measure_height_my", "c_measurement", "height", "measurement", "cm", "absolute",
                    re.compile(rf"\bmy\s+height\s+is\s+(?P<n>{_NUM_TOKEN})\s+(?P<unit>{_HEIGHT_UNIT})\b",
                               re.IGNORECASE),
                    unit_from=_unit_from(_HEIGHT_UNIT_TO_CANON)),
    # ---- c_percent: explicit self completion ------------------------------------------------
    SurfaceTemplate("percent_complete", "c_percent", "degree_progress", "measurement", "percent",
                    "absolute",
                    re.compile(rf"\bi\s+am\s+(?P<n>{_NUM_TOKEN})\s*%\s+(?:done|complete)\s+with\s+my\s+(?P<work>degree|thesis)\b",
                               re.IGNORECASE),
                    attribute_from=lambda m: f"{m.group('work').lower()}_progress"),
    # ---- c_clock_time: standing-property times only (no event times) -------------------------
    SurfaceTemplate("clock_wake_at", "c_clock_time", "wake_time", "clock_time", "", "absolute",
                    re.compile(rf"(?<!now\s)\bi\s+wake\s+up\s+at\s+{_CLOCK}\b", re.IGNORECASE),
                    value_from=_clock_value_or_raise),
    SurfaceTemplate("clock_wake_my", "c_clock_time", "wake_time", "clock_time", "", "absolute",
                    re.compile(rf"\bmy\s+wake\s+time\s+is\s+{_CLOCK}\b", re.IGNORECASE),
                    value_from=_clock_value_or_raise),
    SurfaceTemplate("clock_bed_at", "c_clock_time", "bed_time", "clock_time", "", "absolute",
                    re.compile(rf"(?<!now\s)\bi\s+go\s+to\s+bed\s+at\s+{_CLOCK}\b", re.IGNORECASE),
                    value_from=_clock_value_or_raise),
    SurfaceTemplate("clock_bed_my", "c_clock_time", "bed_time", "clock_time", "", "absolute",
                    re.compile(rf"\bmy\s+bedtime\s+is\s+{_CLOCK}\b", re.IGNORECASE),
                    value_from=_clock_value_or_raise),
    # ---- c_duration: standing self durations -------------------------------------------------
    SurfaceTemplate("duration_commute", "c_duration", "commute_duration", "duration", "minutes",
                    "absolute",
                    re.compile(rf"\bmy\s+commute\s+takes\s+(?P<n>{_NUM_TOKEN})\s+(?P<unit>minutes?|hours?)\b",
                               re.IGNORECASE),
                    unit_from=_unit_from(_DURATION_UNIT_TO_CANON)),
    SurfaceTemplate("duration_workout", "c_duration", "workout_duration", "duration", "minutes",
                    "absolute",
                    re.compile(rf"\bmy\s+workout\s+lasts\s+(?P<n>{_NUM_TOKEN})\s+(?P<unit>minutes?|hours?)\b",
                               re.IGNORECASE),
                    unit_from=_unit_from(_DURATION_UNIT_TO_CANON)),
    SurfaceTemplate("duration_shift", "c_duration", "shift_duration", "duration", "hours",
                    "absolute",
                    re.compile(rf"\bmy\s+shift\s+is\s+(?P<n>{_NUM_TOKEN})\s+hours?\b",
                               re.IGNORECASE)),
    SurfaceTemplate("duration_sleep", "c_duration", "sleep_duration", "duration", "hours",
                    "absolute",
                    re.compile(rf"\bi\s+sleep\s+(?P<n>{_NUM_TOKEN})\s+hours?\s+(?:a|per)\s+night\b",
                               re.IGNORECASE)),
    # ---- c_frequency: explicit rates and intervals -------------------------------------------
    SurfaceTemplate("frequency_rate", "c_frequency", "gym_frequency", "frequency", "week",
                    "absolute",
                    re.compile(rf"\bi\s+(?P<activity>go\s+to\s+the\s+gym|run|swim)\s+(?P<n>{_NUM_TOKEN})\s+times?\s+(?:a|per)\s+(?P<period>day|week|month|year)s?\b",
                               re.IGNORECASE),
                    attribute_from=lambda m: _ACTIVITY_TO_SLOT[m.group("activity").lower()],
                    unit_from=lambda m: _PERIOD_TO_CANON[(m.group("period") or "").lower()]),
    SurfaceTemplate("frequency_interval", "c_frequency", "gym_frequency", "frequency", "",
                    "absolute",
                    re.compile(rf"\bi\s+(?P<activity>go\s+to\s+the\s+gym|run|swim)\s+every\s+(?:(?:other|(?P<c>{_NUM_TOKEN}))\s+)?(?P<period>day|week|month|year)s?\b",
                               re.IGNORECASE),
                    attribute_from=lambda m: _ACTIVITY_TO_SLOT[m.group("activity").lower()],
                    value_from=_interval_placeholder_value),
    # ---- c_delta_add / c_delta_sub: held-slot changes with explicit accumulator context ------
    # A bare change verb ("I added 5 coins") is NOT admitted: the grounded span must name the
    # held slot/accumulator the quantity moved into or out of.
    SurfaceTemplate("delta_add", "c_delta_add", "coins", "count", "", "delta",
                    re.compile(rf"\bi\s+added\s+(?P<n>{_NUM_TOKEN})\s+{_NOUN_GROUP}\s+to\s+my\s+{_ACCUMULATOR_PHRASE}\b",
                               re.IGNORECASE),
                    attribute_from=lambda m: m.group("noun")),
    SurfaceTemplate("delta_sold", "c_delta_sub", "coins", "count", "", "delta",
                    re.compile(rf"\bi\s+sold\s+(?P<n>{_NUM_TOKEN})\s+{_NOUN_GROUP}\s+from\s+my\s+{_ACCUMULATOR_PHRASE}\b",
                               re.IGNORECASE),
                    attribute_from=lambda m: m.group("noun"),
                    value_from=lambda m: -_parse_number(m.group("n"))),
    SurfaceTemplate("delta_gave_away", "c_delta_sub", "coins", "count", "", "delta",
                    re.compile(rf"\bi\s+gave\s+away\s+(?P<n>{_NUM_TOKEN})\s+{_NOUN_GROUP}\s+from\s+my\s+{_ACCUMULATOR_PHRASE}\b",
                               re.IGNORECASE),
                    attribute_from=lambda m: m.group("noun"),
                    value_from=lambda m: -_parse_number(m.group("n"))),
    # ---- c_expire: values that ENDED (used-to forms only) ------------------------------------
    SurfaceTemplate("expire_wake", "c_expire", "wake_time", "clock_time", "", "expire",
                    re.compile(rf"\bi\s+used\s+to\s+wake\s+up\s+at\s+{_CLOCK}\b", re.IGNORECASE),
                    value_from=_clock_value_or_raise),
    SurfaceTemplate("expire_bed", "c_expire", "bed_time", "clock_time", "", "expire",
                    re.compile(rf"\bi\s+used\s+to\s+go\s+to\s+bed\s+at\s+{_CLOCK}\b", re.IGNORECASE),
                    value_from=_clock_value_or_raise),
    SurfaceTemplate("expire_weight", "c_expire", "weight", "measurement", "kg", "expire",
                    re.compile(rf"\bi\s+used\s+to\s+weigh\s+(?P<n>{_NUM_TOKEN})\s+(?P<unit>{_MASS_UNIT})\b",
                               re.IGNORECASE),
                    unit_from=_unit_from(_MASS_UNIT_TO_CANON)),
    SurfaceTemplate("expire_have", "c_expire", "coins", "count", "", "expire",
                    re.compile(rf"\bi\s+used\s+to\s+have\s+(?P<n>{_NUM_TOKEN})\s+{_NOUN_GROUP}\b",
                               re.IGNORECASE),
                    attribute_from=lambda m: m.group("noun")),
)
