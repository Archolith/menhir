"""Regression contract for conservative status identity canonicalization."""

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


def _row(*, value: str, span: str, unit: str = "") -> dict:
    return {
        "episode": 0,
        "subject": "user",
        "attribute": "employment_status",
        "scope": "",
        "value_kind": "status",
        "unit": unit,
        "operation": "absolute",
        "value": value,
        "when": "",
        "stated_span": span,
    }


@pytest.mark.unit
@pytest.mark.parametrize("model_value", ["employed", "Employed", "EMPLOYED", " employed "])
def test_status_value_is_lowercase_canonical_before_identity(model_value):
    span = "I am employed"
    ep = [_Ep(uuid="status-case", content=span)]
    out = extract_typed_scalars_once(ep, _llm([_row(value=model_value, span=span)]))
    assert len(out) == 1
    assert out[0].value == "employed"
    assert out[0].normalized_value == "employed"


@pytest.mark.unit
def test_status_case_variants_converge_before_gate():
    span = "I am employed"
    ep = [_Ep(uuid="status-vote", content=span)]
    samples = [
        extract_typed_scalars_once(ep, _llm([_row(value=value, span=span)]))
        for value in ("employed", "Employed", "EMPLOYED")
    ]
    assert [sample[0].value for sample in samples] == ["employed", "employed", "employed"]
    decisions = gate_typed_scalars(samples)
    assert len(decisions) == 1
    assert decisions[0].committed is True
    assert decisions[0].agreement == 1.0
    assert decisions[0].proposal is not None
    assert decisions[0].proposal.value == "employed"


@pytest.mark.unit
@pytest.mark.parametrize("model_unit", ["state", "category", "status"])
def test_status_forces_blank_unit(model_unit):
    span = "I am employed"
    ep = [_Ep(uuid="status-unit", content=span)]
    out = extract_typed_scalars_once(
        ep,
        _llm([_row(value="employed", span=span, unit=model_unit)]),
    )
    assert len(out) == 1
    assert out[0].unit == ""
    assert out[0].slot_key == ("employment_status", "", "status", "")


@pytest.mark.unit
def test_status_does_not_apply_synonym_ontology():
    """Lowercasing is lexical only: distinct open-world status strings stay distinct."""
    span = "I am working"
    ep = [_Ep(uuid="status-open", content=span)]
    out = extract_typed_scalars_once(ep, _llm([_row(value="working", span=span)]))
    assert len(out) == 1
    assert out[0].value == "working"
    assert out[0].value != "employed"


@pytest.mark.unit
@pytest.mark.parametrize("bad_value", ["", "   "])
def test_blank_status_still_fails_closed(bad_value):
    span = "I am employed"
    ep = [_Ep(uuid="status-blank", content=span)]
    out = extract_typed_scalars_once(ep, _llm([_row(value=bad_value, span=span)]))
    assert out == []
