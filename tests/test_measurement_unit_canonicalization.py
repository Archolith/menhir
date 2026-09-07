"""Regression contract for source-authoritative measurement units.

Measurement unit participates in typed-scalar vote and durable slot identity. Explicit grounded
unit wording must therefore canonicalize before voting/persistence so equivalent spellings do not
scatter and a model-supplied wrong unit cannot override the source.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from menhir.services.typed_scalar_perception import (
    extract_typed_scalars_once,
    gate_typed_scalars,
)


@dataclass(frozen=True)
class _Ep:
    uuid: str
    content: str


def _llm(rows):
    def complete(system: str, user: str) -> str:
        return json.dumps(rows)
    return complete


def _row(*, attribute="weight", unit="kg", value=70, stated_span="I weigh 70 kilograms") -> dict:
    return {
        "episode": 0,
        "subject": "user",
        "attribute": attribute,
        "scope": "",
        "value_kind": "measurement",
        "unit": unit,
        "operation": "absolute",
        "value": value,
        "when": "",
        "stated_span": stated_span,
    }


@pytest.mark.unit
@pytest.mark.parametrize("model_unit", ["kg", "kilogram", "kilograms", "kilo", "kilos"])
def test_weight_source_unit_canonicalizes_to_kg(model_unit):
    span = "I weigh 70 kilograms"
    ep = [_Ep(uuid="measure-kg", content=span)]

    out = extract_typed_scalars_once(ep, _llm([_row(unit=model_unit, stated_span=span)]))

    assert len(out) == 1
    assert out[0].value == 70
    assert out[0].unit == "kg"
    assert out[0].slot_key == ("weight", "", "measurement", "kg")


@pytest.mark.unit
@pytest.mark.parametrize(
    "source_unit",
    ["cm", "centimeter", "centimeters", "centimetre", "centimetres"],
)
def test_height_source_unit_canonicalizes_to_cm(source_unit):
    span = f"my height is 180 {source_unit}"
    ep = [_Ep(uuid=f"measure-cm-{source_unit}", content=span)]
    row = _row(
        attribute="height",
        unit=source_unit,
        value=180,
        stated_span=span,
    )

    out = extract_typed_scalars_once(ep, _llm([row]))

    assert len(out) == 1
    assert out[0].unit == "cm"
    assert out[0].slot_key == ("height", "", "measurement", "cm")


@pytest.mark.unit
def test_grounded_measurement_unit_overrides_wrong_model_unit():
    span = "I weigh 70 kg"
    ep = [_Ep(uuid="measure-wrong-unit", content=span)]

    out = extract_typed_scalars_once(
        ep,
        _llm([_row(unit="lb", stated_span=span)]),
    )

    assert len(out) == 1
    assert out[0].unit == "kg"


@pytest.mark.unit
def test_measurement_unit_synonyms_do_not_scatter_k_sample_vote():
    span = "I weigh 70 kilograms"
    ep = [_Ep(uuid="measure-vote", content=span)]
    samples = [
        extract_typed_scalars_once(ep, _llm([_row(unit=unit, stated_span=span)]))
        for unit in ("kg", "kilograms", "kilos")
    ]

    assert [sample[0].unit for sample in samples] == ["kg", "kg", "kg"]
    decisions = gate_typed_scalars(samples)
    assert len(decisions) == 1
    assert decisions[0].committed is True
    assert decisions[0].agreement == 1.0
    assert decisions[0].proposal is not None
    assert decisions[0].proposal.unit == "kg"


@pytest.mark.unit
def test_measurement_without_grounded_unit_does_not_invent_model_unit():
    span = "my weight is 70"
    ep = [_Ep(uuid="measure-no-unit", content=span)]

    out = extract_typed_scalars_once(
        ep,
        _llm([_row(unit="kg", stated_span=span)]),
    )

    assert out == []


@pytest.mark.unit
def test_unknown_grounded_measurement_unit_fails_closed():
    span = "my weight is 70 kilogramz"
    ep = [_Ep(uuid="measure-unknown-unit", content=span)]

    out = extract_typed_scalars_once(
        ep,
        _llm([_row(unit="kg", stated_span=span)]),
    )

    assert out == []
