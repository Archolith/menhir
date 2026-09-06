"""Regression contract for money currency and decimal canonicalization.

Money currency participates in typed-scalar slot/vote identity. Explicit source currency must
therefore be canonicalized from the grounded span before a proposal reaches the k-sample gate or
persistence, just as interval frequency is source-authoritative today.

Money values must also remain decimal-exact. Binary floats are appropriate for approximate scalar
kinds such as measurements, but money participates in durable identity and arithmetic; $0.10 +
$0.20 must converge exactly with a directly stated $0.30 rather than producing a binary-float tail.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal

import pytest

from menhir.domain.typed_assertion import normalize_scalar
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
    assert out[0].value == Decimal("500")
    assert isinstance(out[0].value, Decimal)
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
    assert out[0].value == Decimal("500")


@pytest.mark.unit
def test_source_currency_prevents_k_sample_vote_scatter():
    ep = [_Ep(uuid="money-vote", content="my savings balance is $500")]
    model_units = ["usd", "dollars", ""]

    samples = [
        extract_typed_scalars_once(ep, _llm([_row(unit=unit)]))
        for unit in model_units
    ]

    assert [sample[0].unit for sample in samples] == ["usd", "usd", "usd"]
    assert all(sample[0].value == Decimal("500") for sample in samples)
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
    assert out[0].value == Decimal("1200")
    assert isinstance(out[0].value, Decimal)
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


@pytest.mark.unit
@pytest.mark.parametrize(
    ("source", "model_value", "expected"),
    [
        ("my savings balance is $0.10", "0.10", Decimal("0.10")),
        ("my savings balance is $19.99", "19.99", Decimal("19.99")),
        ("my savings balance is $1,200.50", "$1,200.50", Decimal("1200.50")),
        ("my savings balance is 0.30 dollars", "0.30", Decimal("0.30")),
    ],
)
def test_money_values_are_parsed_as_exact_decimals(source, model_value, expected):
    ep = [_Ep(uuid="money-decimal", content=source)]
    out = extract_typed_scalars_once(
        ep,
        _llm([_row(unit="usd", value=model_value, stated_span=source)]),
    )

    assert len(out) == 1
    assert out[0].value == expected
    assert isinstance(out[0].value, Decimal)


@pytest.mark.unit
def test_decimal_money_normalization_is_canonical_and_exact():
    assert normalize_scalar(Decimal("0.10")) == "0.10"
    assert normalize_scalar(Decimal("0.20")) == "0.20"
    assert normalize_scalar(Decimal("0.30")) == "0.30"
    assert normalize_scalar(Decimal("1200.50")) == "1200.50"


@pytest.mark.unit
def test_money_decimal_arithmetic_converges_with_directly_stated_value():
    # The money domain must be able to fold/add without binary-float identity drift.
    start = Decimal("0.10")
    delta = Decimal("0.20")
    direct = Decimal("0.30")

    folded = start + delta

    assert folded == direct
    assert normalize_scalar(folded) == normalize_scalar(direct) == "0.30"


@pytest.mark.unit
def test_non_money_numeric_scalars_are_not_forced_to_decimal():
    # The fix is money-specific; approximate scalar kinds keep their existing numeric contract.
    ep = [_Ep(uuid="measurement-float", content="my weight is 70.5 kg")]
    row = {
        "episode": 0,
        "subject": "user",
        "attribute": "weight",
        "scope": "",
        "value_kind": "measurement",
        "unit": "kg",
        "operation": "absolute",
        "value": 70.5,
        "when": "",
        "stated_span": "my weight is 70.5 kg",
    }

    out = extract_typed_scalars_once(ep, _llm([row]))

    assert len(out) == 1
    assert out[0].value == 70.5
    assert isinstance(out[0].value, float)
