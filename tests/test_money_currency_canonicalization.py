"""Regression contract for money currency canonicalization.

Money currency participates in typed-scalar slot/vote identity. Explicit source currency must
therefore be canonicalized from the grounded span before a proposal reaches the k-sample gate or
persistence, just as interval frequency is source-authoritative today.
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


def _row(*, unit: str, value=500, stated_span="my savings balance is $500") -> dict:
    return {
        "episode": 0,
        "subject": "user",
        "attribute": "savings_balance",
        "scope": "",
        "value_kind": "money",
        "unit": unit,
        "operation": "absolute",
        "value": value,
        "when": "",
        "stated_span": stated_span,
    }


@pytest.mark.unit
@pytest.mark.parametrize("model_unit", ["usd", "USD", "dollar", "dollars", "$", ""])
def test_explicit_dollar_source_canonicalizes_to_usd(model_unit):
    ep = [_Ep(uuid="money-usd", content="my savings balance is $500")]

    out = extract_typed_scalars_once(ep, _llm([_row(unit=model_unit)]))

    assert len(out) == 1
    assert out[0].value == 500
    assert out[0].unit == "usd"
    assert out[0].slot_key == ("savings_balance", "", "money", "usd")


@pytest.mark.unit
@pytest.mark.parametrize("model_unit", ["usd", "USD", "dollar", "dollars", "$"])
def test_explicit_dollars_wording_canonicalizes_to_usd(model_unit):
    span = "my savings balance is 500 dollars"
    ep = [_Ep(uuid="money-dollars", content=span)]

    out = extract_typed_scalars_once(
        ep,
        _llm([_row(unit=model_unit, stated_span=span)]),
    )

    assert len(out) == 1
    assert out[0].unit == "usd"


@pytest.mark.unit
def test_source_currency_prevents_k_sample_vote_scatter():
    ep = [_Ep(uuid="money-vote", content="my savings balance is $500")]
    model_units = ["usd", "dollars", ""]

    samples = [
        extract_typed_scalars_once(ep, _llm([_row(unit=unit)]))
        for unit in model_units
    ]

    assert [sample[0].unit for sample in samples] == ["usd", "usd", "usd"]
    decisions = gate_typed_scalars(samples)
    assert len(decisions) == 1
    assert decisions[0].committed is True
    assert decisions[0].agreement == 1.0
    assert decisions[0].proposal is not None
    assert decisions[0].proposal.unit == "usd"


@pytest.mark.unit
def test_explicit_currency_overrides_wrong_model_currency():
    ep = [_Ep(uuid="money-wrong-unit", content="my checking balance is $1,200")]
    row = _row(
        unit="eur",
        value="$1,200",
        stated_span="my checking balance is $1,200",
    )
    row["attribute"] = "checking_balance"

    out = extract_typed_scalars_once(ep, _llm([row]))

    assert len(out) == 1
    assert out[0].value == 1200
    assert out[0].unit == "usd"


@pytest.mark.unit
def test_money_without_grounded_currency_does_not_invent_usd():
    span = "my account balance is 500"
    ep = [_Ep(uuid="money-ambiguous", content=span)]

    out = extract_typed_scalars_once(
        ep,
        _llm([_row(unit="usd", stated_span=span)]),
    )

    assert out == []
