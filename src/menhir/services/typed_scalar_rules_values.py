"""Kind-typed value coercion and source-derived values for the typed-scalar boundary.

Light JSON-value coercion, decimal/money/duration/measurement/clock/count canonicalization, and
source-polarity checks. Moved verbatim from ``typed_scalar_rules``; the facade module re-exports
every name so existing import sites keep working unchanged.
"""

from __future__ import annotations

import math
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from menhir.domain.typed_assertion import NUMERIC_KINDS
from menhir.services.typed_scalar_rules_canon import _FREQUENCY_NUMBER_WORDS
from menhir.services.typed_scalar_rules_temporal import _SRC_ISO_RE, _SRC_MONTH_DAY_YEAR_RE

_DURATION_M_SS_RE = re.compile(
    r"^(?P<sign>[+-]?)(?P<minutes>\d+):(?P<seconds>[0-5]\d(?:\.\d+)?)$"
)
_DURATION_H_MM_SS_RE = re.compile(
    r"^(?P<sign>[+-]?)(?P<hours>\d+):(?P<minutes>[0-5]\d):"
    r"(?P<seconds>[0-5]\d(?:\.\d+)?)$"
)

_DURATION_UNIT_SECONDS: dict[str, Decimal] = {
    "second": Decimal("1"), "seconds": Decimal("1"),
    "minute": Decimal("60"), "minutes": Decimal("60"),
    "hour": Decimal("3600"), "hours": Decimal("3600"),
}
_MEASUREMENT_UNIT_ALIASES: dict[str, str] = {
    "kg": "kg", "kilo": "kg", "kilos": "kg",
    "kilogram": "kg", "kilograms": "kg",
    "cm": "cm", "centimeter": "cm", "centimeters": "cm",
    "centimetre": "cm", "centimetres": "cm",
    "km": "km", "kilometer": "km", "kilometers": "km",
    "kilometre": "km", "kilometres": "km",
    "mi": "miles", "mile": "miles", "miles": "miles",
    "%": "percent", "percent": "percent", "percentage": "percent",
}
_SOURCE_NUMBER = r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
_MEASUREMENT_AMOUNT_UNIT_RE = re.compile(
    rf"(?<![\w.]){_SOURCE_NUMBER}\s*(?:(?P<symbol>%)|(?P<word>[A-Za-z]+)\b)",
    re.IGNORECASE,
)
_CLOCK_SOURCE_RE = re.compile(
    r"(?<!\d)(?P<hour>\d{1,2}):(?P<minute>[0-5]\d)\s*(?P<meridiem>am|pm)?\b",
    re.IGNORECASE,
)
_USD_SOURCE_RE = re.compile(r"\$|\b(?:usd|dollars?)\b", re.IGNORECASE)
_BOOLEAN_UNCERTAIN_RE = re.compile(
    r"\b(?:no\s+longer|used\s+to|did(?:\s+not|n't)\s+use\s+to|might|may|could|perhaps)\b|"
    r"\b(?:do\s+not|don't)\s+think\b|\bnot\s+sure\b",
    re.IGNORECASE,
)
_BOOLEAN_NEGATIVE_RE = re.compile(
    r"\b(?:do\s+not|don't|does\s+not|doesn't)\b|\b(?:am|is|are)\s+not\b",
    re.IGNORECASE,
)
_BOOLEAN_POSITIVE_RE = re.compile(
    r"\b(?:i|we|you|they|he|she|it)\s+(?:have|has)\b",
    re.IGNORECASE,
)



def _coerce_value(value_kind: str, operation: str, raw: Any) -> Any:
    """Light coercion of a JSON value into its kind-typed shape BEFORE `validate_value` enforces it.
    Native JSON types mostly arrive correct (bool, number, [lo, hi], string); this only rescues the
    common near-misses — a numeric kind quoted as a string, a range given as a list — and normalizes
    strings. Returns the coerced value (which `validate_value` then accepts or rejects); it never
    fabricates a value it wasn't given."""
    if value_kind == "boolean":
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str) and raw.strip().lower() in ("true", "false"):
            return raw.strip().lower() == "true"
        return raw  # let validate_value reject anything else
    if value_kind in NUMERIC_KINDS:
        if operation == "delta":
            return _as_number(raw)
        if isinstance(raw, (list, tuple)) and len(raw) == 2:
            return [_as_number(raw[0]), _as_number(raw[1])]
        return _as_number(raw)
    # status / weekday / clock_time: string kinds
    if isinstance(raw, str):
        return raw.strip()
    return raw


def _as_number(raw: Any) -> Any:
    """Best-effort numeric coercion: an int stays int, a float stays float, a numeric string becomes
    a number; anything else is returned unchanged so `validate_value` can reject it."""
    if isinstance(raw, bool):
        return raw  # a bool is not a number here; validate_value will reject it for a numeric kind
    if isinstance(raw, (int, float)):
        return raw
    if isinstance(raw, Decimal):
        if not raw.is_finite():
            return raw
        return int(raw) if raw == raw.to_integral_value() else float(raw)
    if isinstance(raw, str):
        s = raw.strip().replace("$", "").replace(",", "")
        try:
            f = float(s)
        except (TypeError, ValueError):
            return raw
        return int(f) if f.is_integer() else f
    return raw


def _duration_colon_seconds(raw: Any, *, allow_signed: bool = False) -> int | float | None:
    """Normalize an elapsed ``M:SS`` or ``H:MM:SS`` string to seconds.

    A colon is ambiguous in untyped prose, but it is not ambiguous after the model has classified
    the value as ``duration``. Canonicalizing here keeps the domain contract numeric, makes
    equivalent renderings vote together, and preserves the original text in ``stated_span`` for
    provenance. Seconds (and, for the three-part form, minutes) must be 00-59; malformed values
    return None and continue through the ordinary fail-closed validator.
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    match = _DURATION_M_SS_RE.fullmatch(text)
    hours = 0
    if match is None:
        match = _DURATION_H_MM_SS_RE.fullmatch(text)
        if match is None:
            return None
        hours = int(match.group("hours"))
    sign = match.group("sign")
    if sign and not allow_signed:
        return None
    seconds_text = match.group("seconds")
    seconds: int | float = (
        float(seconds_text) if "." in seconds_text else int(seconds_text)
    )
    total = hours * 3600 + int(match.group("minutes")) * 60 + seconds
    if sign == "-":
        total = -total
    return int(total) if isinstance(total, float) and total.is_integer() else total


def _normalize_colon_duration_value(raw: Any, operation: str) -> tuple[Any, bool]:
    """Return ``(value, normalized)`` for a complete colon-duration value or range.

    Absolute/expire values accept an unsigned point or two-endpoint range. Deltas accept one signed
    point. A mixed range (one colon endpoint and one ordinary number) is deliberately not converted:
    changing its unit to seconds would silently reinterpret the numeric endpoint.
    """
    if operation != "delta" and isinstance(raw, (list, tuple)) and len(raw) == 2:
        normalized_range = [
            _duration_colon_seconds(endpoint, allow_signed=False) for endpoint in raw
        ]
        if all(endpoint is not None for endpoint in normalized_range):
            return normalized_range, True
        return raw, False
    normalized = _duration_colon_seconds(raw, allow_signed=operation == "delta")
    return (normalized, True) if normalized is not None else (raw, False)


def _decimal_number(raw: Any) -> Decimal | None:
    """Parse one finite decimal without routing through binary float arithmetic."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, Decimal):
        return raw if raw.is_finite() else None
    if isinstance(raw, int):
        return Decimal(raw)
    if isinstance(raw, float):
        return Decimal(str(raw)) if math.isfinite(raw) else None
    if not isinstance(raw, str):
        return None
    cleaned = raw.strip().replace("$", "").replace(",", "")
    if not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)", cleaned):
        return None
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        return None
    return value if value.is_finite() else None


def _money_value(raw: Any, operation: str) -> Any:
    """Return a Decimal money point/range, or the original shape for fail-closed validation."""
    if operation != "delta" and isinstance(raw, (list, tuple)) and len(raw) == 2:
        values = [_decimal_number(item) for item in raw]
        return values if all(item is not None for item in values) else raw
    parsed = _decimal_number(raw)
    return parsed if parsed is not None else raw


def _duration_value(raw: Any, unit: str, operation: str) -> tuple[Any, str] | None:
    """Canonicalize every admitted elapsed duration to numeric seconds."""
    colon_value, colon_normalized = _normalize_colon_duration_value(raw, operation)
    if colon_normalized:
        return colon_value, "seconds"

    factor = _DURATION_UNIT_SECONDS.get(unit.strip().lower())
    if factor is None:
        return None
    coerced = _coerce_value("duration", operation, raw)
    values = coerced if isinstance(coerced, (list, tuple)) else [coerced]
    converted: list[int | float] = []
    for item in values:
        number = _decimal_number(item)
        if number is None:
            return None
        seconds = number * factor
        converted.append(int(seconds) if seconds == seconds.to_integral_value() else float(seconds))
    value: Any = converted if isinstance(coerced, (list, tuple)) else converted[0]
    return value, "seconds"


def _money_currency_from_source(stated_span: str) -> str | None:
    """Return the canonical currency only when the grounded quote states one explicitly."""
    return "usd" if _USD_SOURCE_RE.search(stated_span or "") else None


def _measurement_unit_from_source(stated_span: str) -> str | None:
    """Resolve a closed lexical measurement-unit alias from the grounded quote."""
    units: list[str] = []
    for match in _MEASUREMENT_AMOUNT_UNIT_RE.finditer(stated_span or ""):
        token = match.group("symbol") or match.group("word") or ""
        token = token.lower()
        # Range connectors after the first endpoint are not units ("between 1 and 2 kg").
        if token in {"and", "or", "to"}:
            continue
        canonical = _MEASUREMENT_UNIT_ALIASES.get(token)
        if canonical is None:
            return None
        units.append(canonical)
    return units[0] if units and len(set(units)) == 1 else None


def _clock_time_from_source(stated_span: str) -> str | None:
    """Parse exactly one source clock and canonicalize it to 24-hour HH:MM."""
    matches = list(_CLOCK_SOURCE_RE.finditer(stated_span or ""))
    if len(matches) != 1:
        return None
    match = matches[0]
    hour = int(match.group("hour"))
    minute = int(match.group("minute"))
    meridiem = (match.group("meridiem") or "").lower()
    if meridiem:
        if not 1 <= hour <= 12:
            return None
        if meridiem == "pm" and hour < 12:
            hour += 12
        elif meridiem == "am" and hour == 12:
            hour = 0
    elif not 0 <= hour <= 23:
        return None
    return f"{hour:02d}:{minute:02d}"


_COUNT_WORDS = dict(_FREQUENCY_NUMBER_WORDS)
_COUNT_TOKEN_RE = re.compile(
    rf"(?<![\w.])(?P<token>{_SOURCE_NUMBER}|{'|'.join(_COUNT_WORDS)})(?![\w.])",
    re.IGNORECASE,
)
_COUNT_RANGE_RE = re.compile(
    rf"\b(?:between|from)\s+(?P<low>{_SOURCE_NUMBER}|{'|'.join(_COUNT_WORDS)})\s+"
    rf"(?:and|to)\s+(?P<high>{_SOURCE_NUMBER}|{'|'.join(_COUNT_WORDS)})\b",
    re.IGNORECASE,
)
_COUNT_NEGATIVE_DELTA_RE = re.compile(
    r"\b(?:sold|lost|removed|subtracted|decreased|gave\s+away)\b", re.IGNORECASE,
)
_COUNT_POSITIVE_DELTA_RE = re.compile(
    r"\b(?:added|gained|bought|increased|received)\b", re.IGNORECASE,
)


def _count_token_value(token: str) -> int | Decimal:
    normalized = token.strip().lower()
    if normalized in _COUNT_WORDS:
        return _COUNT_WORDS[normalized]
    number = Decimal(normalized.replace(",", ""))
    return int(number) if number == number.to_integral_value() else number


def _count_value_from_source(stated_span: str, operation: str, model_value: Any) -> Any | None:
    """Derive a discrete count from one explicit source number, failing closed on ambiguity."""
    # Calendar numbers describe validity, not quantity. Temporal resolution owns them.
    span = _SRC_ISO_RE.sub(" ", _SRC_MONTH_DAY_YEAR_RE.sub(" ", stated_span or ""))
    range_match = _COUNT_RANGE_RE.search(span)
    if operation != "delta" and range_match is not None:
        return [
            _count_token_value(range_match.group("low")),
            _count_token_value(range_match.group("high")),
        ]

    tokens = [match.group("token") for match in _COUNT_TOKEN_RE.finditer(span)]
    if len(tokens) != 1:
        return None
    value = _count_token_value(tokens[0])
    if operation != "delta":
        return value
    magnitude = abs(value)
    if _COUNT_NEGATIVE_DELTA_RE.search(span):
        return -magnitude
    if _COUNT_POSITIVE_DELTA_RE.search(span):
        return magnitude
    if str(tokens[0]).lstrip().startswith("-"):
        return -magnitude
    coerced_model = _as_number(model_value)
    return -magnitude if isinstance(coerced_model, (int, float)) and coerced_model < 0 else magnitude


def _boolean_source_polarity(stated_span: str) -> bool | None:
    """Classify only closed, unambiguous present boolean forms; otherwise abstain."""
    span = (stated_span or "").strip()
    if not span or _BOOLEAN_UNCERTAIN_RE.search(span):
        return None
    if _BOOLEAN_NEGATIVE_RE.search(span):
        return False
    if _BOOLEAN_POSITIVE_RE.search(span):
        return True
    return None
