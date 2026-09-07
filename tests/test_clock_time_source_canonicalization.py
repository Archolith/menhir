"""Regression contract for source-authoritative clock-time canonicalization."""

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


def _row(*, value: str, stated_span: str, unit: str = "") -> dict:
    return {
        "episode": 0,
        "subject": "user",
        "attribute": "wake_time",
        "scope": "",
        "value_kind": "clock_time",
        "unit": unit,
        "operation": "absolute",
        "value": value,
        "when": "",
        "stated_span": stated_span,
    }


@pytest.mark.unit
@pytest.mark.parametrize(
    ("span", "model_value", "expected"),
    [
        ("wake at 7:30 PM", "07:30", "19:30"),
        ("wake at 7:30 pm", "19:30", "19:30"),
        ("wake at 12:15 AM", "12:15", "00:15"),
        ("wake at 12:15 PM", "00:15", "12:15"),
        ("wake at 7:30", "7:30", "07:30"),
        ("wake at 22:15", "22:15", "22:15"),
    ],
)
def test_grounded_clock_source_overrides_model_value(span, model_value, expected):
    ep = [_Ep(uuid="clock-source", content=f"I {span}")]
    out = extract_typed_scalars_once(ep, _llm([_row(value=model_value, stated_span=span)]))
    assert len(out) == 1
    assert out[0].value == expected
    assert out[0].unit == ""


@pytest.mark.unit
def test_grounded_pm_source_prevents_vote_scatter():
    span = "wake at 7:30 PM"
    ep = [_Ep(uuid="clock-vote", content=f"I {span}")]
    samples = [
        extract_typed_scalars_once(ep, _llm([_row(value=value, stated_span=span)]))
        for value in ("07:30", "19:30", "7:30")
    ]
    assert [sample[0].value for sample in samples] == ["19:30", "19:30", "19:30"]
    decisions = gate_typed_scalars(samples)
    assert len(decisions) == 1
    assert decisions[0].committed is True
    assert decisions[0].agreement == 1.0
    assert decisions[0].proposal is not None
    assert decisions[0].proposal.value == "19:30"


@pytest.mark.unit
@pytest.mark.parametrize("model_unit", ["hours", "local_time", "clock"])
def test_clock_time_forces_blank_unit(model_unit):
    span = "wake at 07:30"
    ep = [_Ep(uuid="clock-unit", content=f"I {span}")]
    out = extract_typed_scalars_once(
        ep,
        _llm([_row(value="07:30", stated_span=span, unit=model_unit)]),
    )
    assert len(out) == 1
    assert out[0].unit == ""
    assert out[0].slot_key == ("wake_time", "", "clock_time", "")


@pytest.mark.unit
@pytest.mark.parametrize(
    "span",
    [
        "wake at 22:15 PM",
        "wake at 00:15 am",
        "wake at 13:15 AM",
    ],
)
def test_invalid_mixed_24h_meridiem_source_fails_closed(span):
    ep = [_Ep(uuid="clock-invalid", content=f"I {span}")]
    out = extract_typed_scalars_once(ep, _llm([_row(value="22:15", stated_span=span)]))
    assert out == []


@pytest.mark.unit
def test_clock_time_without_grounded_clock_does_not_trust_model_value():
    span = "my wake time is early"
    ep = [_Ep(uuid="clock-ungrounded-value", content=span)]
    out = extract_typed_scalars_once(ep, _llm([_row(value="07:30", stated_span=span)]))
    assert out == []
