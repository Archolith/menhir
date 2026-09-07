"""Regression contract for narrow source/model boolean polarity consistency."""

from __future__ import annotations

import json
from dataclasses import dataclass

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


def _row(*, value: bool, stated_span: str, unit: str = "") -> dict:
    return {
        "episode": 0,
        "subject": "user",
        "attribute": "has_passport",
        "scope": "",
        "value_kind": "boolean",
        "unit": unit,
        "operation": "absolute",
        "value": value,
        "when": "",
        "stated_span": stated_span,
    }


@pytest.mark.unit
@pytest.mark.parametrize(
    "span",
    [
        "I have a passport",
        "I have finished reading Dune",
        "I have completed the course",
    ],
)
def test_unambiguous_positive_boolean_rejects_false_model_value(span):
    ep = [_Ep(uuid="bool-positive", content=span)]
    out = extract_typed_scalars_once(ep, _llm([_row(value=False, stated_span=span)]))
    assert out == []


@pytest.mark.unit
@pytest.mark.parametrize(
    "span",
    [
        "I don't smoke",
        "I do not smoke",
        "I am not a homeowner",
    ],
)
def test_unambiguous_negative_boolean_rejects_true_model_value(span):
    ep = [_Ep(uuid="bool-negative", content=span)]
    out = extract_typed_scalars_once(ep, _llm([_row(value=True, stated_span=span)]))
    assert out == []


@pytest.mark.unit
@pytest.mark.parametrize(
    ("span", "value"),
    [
        ("I have a passport", True),
        ("I have finished reading Dune", True),
        ("I don't smoke", False),
        ("I do not smoke", False),
    ],
)
def test_unambiguous_boolean_matching_source_polarity_is_admitted(span, value):
    ep = [_Ep(uuid="bool-match", content=span)]
    out = extract_typed_scalars_once(ep, _llm([_row(value=value, stated_span=span)]))
    assert len(out) == 1
    assert out[0].value is value
    assert out[0].unit == ""


@pytest.mark.unit
@pytest.mark.parametrize("model_unit", ["bool", "yes_no", "boolean"])
def test_boolean_forces_blank_unit(model_unit):
    span = "I have a passport"
    ep = [_Ep(uuid="bool-unit", content=span)]
    out = extract_typed_scalars_once(
        ep,
        _llm([_row(value=True, stated_span=span, unit=model_unit)]),
    )
    assert len(out) == 1
    assert out[0].unit == ""


@pytest.mark.unit
@pytest.mark.parametrize(
    "span",
    [
        "I no longer smoke",
        "I used to smoke",
        "I didn't use to smoke",
        "I might smoke occasionally",
        "I don't think I smoke",
        "I'm not sure whether I smoke",
    ],
)
def test_ambiguous_or_temporal_boolean_forms_are_not_rewritten_by_narrow_polarity_gate(span):
    """The new helper must abstain from deciding these forms; existing temporal/ambiguity rules own them."""
    ep = [_Ep(uuid="bool-ambiguous", content=span)]
    # This assertion intentionally does not require admission. It requires only that the narrow
    # polarity verifier not manufacture/flip a value. Existing parser guards may admit or drop.
    out = extract_typed_scalars_once(ep, _llm([_row(value=False, stated_span=span)]))
    if out:
        assert out[0].value is False
