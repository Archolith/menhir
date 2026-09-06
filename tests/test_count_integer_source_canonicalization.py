"""Regression contract for discrete count semantics and source-authoritative values."""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from menhir.domain.typed_assertion import validate_value
from menhir.services.typed_scalar_perception import extract_typed_scalars_once, gate_typed_scalars


@dataclass(frozen=True)
class _Ep:
    uuid: str
    content: str


def _llm(rows):
    def complete(system: str, user: str) -> str:
        return json.dumps(rows)
    return complete


def _row(*, value, stated_span="37 coins", unit="", operation="absolute") -> dict:
    return {
        "episode": 0,
        "subject": "user",
        "attribute": "coins",
        "scope": "",
        "value_kind": "count",
        "unit": unit,
        "operation": operation,
        "value": value,
        "when": "",
        "stated_span": stated_span,
    }


@pytest.mark.unit
@pytest.mark.parametrize("value", [3.5, -2.7, [2.2, 5.8], [1, 2.5]])
def test_count_rejects_fractional_values(value):
    with pytest.raises(ValueError):
        validate_value("count", "absolute", value)


@pytest.mark.unit
@pytest.mark.parametrize("value", [0, 1, 37, [2, 5]])
def test_absolute_count_accepts_integer_points_and_ranges(value):
    validate_value("count", "absolute", value)


@pytest.mark.unit
@pytest.mark.parametrize("value", [-3, 0, 4])
def test_count_delta_accepts_signed_integers(value):
    validate_value("count", "delta", value)


@pytest.mark.unit
@pytest.mark.parametrize("value", [-3.5, 2.25])
def test_count_delta_rejects_fractional_values(value):
    with pytest.raises(ValueError):
        validate_value("count", "delta", value)


@pytest.mark.unit
def test_grounded_explicit_count_overrides_wrong_model_value():
    ep = [_Ep(uuid="count-source", content="I have 37 coins")]
    out = extract_typed_scalars_once(
        ep,
        _llm([_row(value=36, stated_span="37 coins")]),
    )
    assert len(out) == 1
    assert out[0].value == 37
    assert out[0].unit == ""


@pytest.mark.unit
def test_grounded_count_prevents_vote_scatter():
    ep = [_Ep(uuid="count-vote", content="I have 37 coins")]
    samples = [
        extract_typed_scalars_once(
            ep,
            _llm([_row(value=value, stated_span="37 coins")]),
        )
        for value in (37, 36, 38)
    ]
    assert [sample[0].value for sample in samples] == [37, 37, 37]
    decisions = gate_typed_scalars(samples)
    assert len(decisions) == 1
    assert decisions[0].committed is True
    assert decisions[0].agreement == 1.0
    assert decisions[0].proposal is not None
    assert decisions[0].proposal.value == 37


@pytest.mark.unit
@pytest.mark.parametrize("model_unit", ["coins", "items", "count"])
def test_count_forces_blank_unit(model_unit):
    ep = [_Ep(uuid="count-unit", content="I have 37 coins")]
    out = extract_typed_scalars_once(
        ep,
        _llm([_row(value=37, stated_span="37 coins", unit=model_unit)]),
    )
    assert len(out) == 1
    assert out[0].unit == ""
    assert out[0].slot_key == ("coins", "", "count", "")


@pytest.mark.unit
def test_count_without_grounded_numeric_token_does_not_trust_model_value():
    span = "I have several coins"
    ep = [_Ep(uuid="count-no-number", content=span)]
    out = extract_typed_scalars_once(
        ep,
        _llm([_row(value=7, stated_span=span)]),
    )
    assert out == []
