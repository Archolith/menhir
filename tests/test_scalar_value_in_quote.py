"""Regression contract for #89: a model-supplied money or measurement value must be stated in its quote.

`_ground_span` only proves the quote occurs exactly once in the episode. For kinds whose value comes
from the model as-is (money, measurement), the quote must also state that number, as digits or words,
or the proposal drops with `value_not_in_span`. Counts, durations, frequencies and clock times are
already derived from the span and keep their existing behavior.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal

import pytest

from menhir.services.typed_scalar_perception import extract_typed_scalars_once


@dataclass(frozen=True)
class _Ep:
    uuid: str
    content: str


def _llm(rows):
    def complete(system: str, user: str) -> str:
        return json.dumps(rows)
    return complete


def _row(*, value_kind: str, value, stated_span: str, unit: str = "") -> dict:
    return {
        "episode": 0,
        "subject": "user",
        "attribute": "some_attribute",
        "scope": "",
        "value_kind": value_kind,
        "unit": unit,
        "operation": "absolute",
        "value": value,
        "when": "",
        "stated_span": stated_span,
    }


def _extract(span: str, value_kind: str, value, unit: str = ""):
    drops: list[str] = []
    out = extract_typed_scalars_once(
        [_Ep(uuid="ep-89", content=f"Just an update: {span}.")],
        _llm([_row(value_kind=value_kind, value=value, stated_span=span, unit=unit)]),
        on_drop=drops.append,
    )
    return out, drops


@pytest.mark.unit
@pytest.mark.parametrize(
    ("span", "value", "expected"),
    [
        ("my savings balance is $900", "900", Decimal("900")),
        ("my savings balance is $1,200", "1200", Decimal("1200")),
        ("my savings balance is $10.00", "10", Decimal("10")),
        ("my savings balance is twenty-five dollars", "25", Decimal("25")),
        ("my savings balance is ten dollars", "10", Decimal("10")),
        ("my savings balance is $5k", "5000", Decimal("5000")),
        ("my house is worth 1.2 million dollars", "1200000", Decimal("1200000")),
    ],
)
def test_money_stated_in_the_quote_commits(span, value, expected):
    out, drops = _extract(span, "money", value)

    assert drops == []
    assert len(out) == 1 and out[0].value == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    ("span", "value"),
    [
        ("my savings balance is $900", "950"),
        ("I'm really proud of the dollars we've saved so far", "5"),
        ("my savings balance is $5k", "5"),
    ],
)
def test_money_missing_from_the_quote_is_dropped(span, value):
    out, drops = _extract(span, "money", value)

    assert out == []
    assert drops == ["value_not_in_span"]


@pytest.mark.unit
def test_measurement_value_must_be_in_the_quote():
    ok, ok_drops = _extract("I weigh 70 kg", "measurement", 70, unit="kg")
    bad, bad_drops = _extract("I weigh 70 kg", "measurement", 75, unit="kg")

    assert ok_drops == [] and len(ok) == 1
    assert bad == [] and bad_drops == ["value_not_in_span"]


@pytest.mark.unit
def test_count_keeps_source_correction():
    # Counts are derived from the quote: the model's 2 is corrected to the stated one.
    out, drops = _extract("cut back to just one cup in the morning", "count", 2)

    assert drops == []
    assert len(out) == 1 and out[0].value == 1


@pytest.mark.unit
def test_count_with_several_numbers_still_abstains():
    out, drops = _extract("I have 25 postcards and 30 books", "count", 25)

    assert out == []
    assert drops == ["count_value_unresolved"]
