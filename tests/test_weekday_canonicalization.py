"""Regression contract for canonical weekday values and unitless identity."""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from menhir.services.typed_scalar_perception import extract_typed_scalars_once, gate_typed_scalars


@dataclass(frozen=True)
class _Ep:
    uuid: str
    content: str


def _llm(rows):
    def complete(system: str, user: str) -> str:
        return json.dumps(rows)
    return complete


def _row(*, value: str, unit: str = "") -> dict:
    return {
        "episode": 0,
        "subject": "user",
        "attribute": "gym_day",
        "scope": "",
        "value_kind": "weekday",
        "unit": unit,
        "operation": "absolute",
        "value": value,
        "when": "",
        "stated_span": "my gym day is Monday",
    }


@pytest.mark.unit
@pytest.mark.parametrize("model_value", ["Monday", "MONDAY", "monday", " Monday "])
def test_weekday_value_is_canonical_lowercase(model_value):
    ep = [_Ep(uuid="weekday-case", content="my gym day is Monday")]
    out = extract_typed_scalars_once(ep, _llm([_row(value=model_value)]))
    assert len(out) == 1
    assert out[0].value == "monday"
    assert out[0].normalized_value == "monday"


@pytest.mark.unit
def test_weekday_case_variants_converge_before_vote():
    ep = [_Ep(uuid="weekday-vote", content="my gym day is Monday")]
    samples = [
        extract_typed_scalars_once(ep, _llm([_row(value=value)]))
        for value in ("Monday", "MONDAY", "monday")
    ]
    assert [sample[0].value for sample in samples] == ["monday", "monday", "monday"]
    decisions = gate_typed_scalars(samples)
    assert len(decisions) == 1
    assert decisions[0].committed is True
    assert decisions[0].agreement == 1.0


@pytest.mark.unit
@pytest.mark.parametrize("model_unit", ["day", "weekday", "week", "calendar_day"])
def test_weekday_forces_blank_unit(model_unit):
    ep = [_Ep(uuid="weekday-unit", content="my gym day is Monday")]
    out = extract_typed_scalars_once(ep, _llm([_row(value="Monday", unit=model_unit)]))
    assert len(out) == 1
    assert out[0].unit == ""
    assert out[0].slot_key == ("gym_day", "", "weekday", "")


@pytest.mark.unit
@pytest.mark.parametrize("bad_value", ["Funday", "", "1", "monday-ish"])
def test_invalid_weekday_values_still_fail_closed(bad_value):
    ep = [_Ep(uuid="weekday-invalid", content="my gym day is Monday")]
    out = extract_typed_scalars_once(ep, _llm([_row(value=bad_value)]))
    assert out == []
