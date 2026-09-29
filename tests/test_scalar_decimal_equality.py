"""Regression contract for GitHub issue #152: scalar Decimal scale/sign canonicalization.

Equal money amounts written with different Decimal scale (10, 10.0, 10.00) must share one
canonical normalized form so the k-sample agreement gate concentrates their votes and the
assertion_key is idempotent. Zero (0 / 0.00 / -0.00) collapses to "0". Distinct values must
stay distinct. No float is ever introduced for Decimals.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal

import pytest

from menhir.domain.typed_assertion import TypedAssertion, normalize_scalar
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


def _money_row(*, value, stated_span: str) -> dict:
    return {
        "episode": 0,
        "subject": "user",
        "attribute": "savings_balance",
        "scope": "",
        "value_kind": "money",
        "unit": "usd",
        "operation": "absolute",
        "value": value,
        "when": "",
        "stated_span": stated_span,
    }


@pytest.mark.unit
def test_num_norm_scale_independent_across_numeric_types():
    assert normalize_scalar(Decimal("10")) == "10"
    assert normalize_scalar(Decimal("10.0")) == "10"
    assert normalize_scalar(Decimal("10.00")) == "10"
    assert normalize_scalar(10) == "10"
    assert normalize_scalar(10.0) == "10"
    assert len({
        normalize_scalar(v) for v in
        (Decimal("10"), Decimal("10.0"), Decimal("10.00"), 10, 10.0)
    }) == 1


@pytest.mark.unit
def test_num_norm_zero_forms_collapse_including_negative_zero():
    for v in (0, 0.0, Decimal("0"), Decimal("0.00"), Decimal("-0"), Decimal("-0.00"), -0.0):
        assert normalize_scalar(v) == "0", v


@pytest.mark.unit
def test_num_norm_exponent_decimal_becomes_plain_notation():
    assert normalize_scalar(Decimal("1E+2")) == normalize_scalar(100) == "100"
    assert normalize_scalar(Decimal("1E+2")) != "1E+2"


@pytest.mark.unit
def test_num_norm_distinct_values_stay_distinct():
    assert normalize_scalar(Decimal("10")) != normalize_scalar(Decimal("10.01"))
    large = Decimal("123456789012345678.90")
    assert normalize_scalar(large) == "123456789012345678.9"
    assert normalize_scalar(large) != normalize_scalar(Decimal("123456789012345679"))
    assert normalize_scalar(Decimal("0.005")) != normalize_scalar(Decimal("0.01"))
    assert normalize_scalar(Decimal("0.005")) == "0.005"


@pytest.mark.unit
def test_assertion_key_identical_across_decimal_scale():
    def _assertion(value) -> TypedAssertion:
        return TypedAssertion(
            subject_uuid="user-1",
            subject_display="user",
            attribute="savings_balance",
            scope="",
            value_kind="money",
            unit="usd",
            operation="absolute",
            value=value,
            stated_span="my savings balance is $10",
            episode_uuid="ep-152",
            valid_at="2026-01-01",
            learned_at="2026-01-02",
            span_start=0,
            span_end=25,
            perceiver_version="v2",
        )

    keys = {
        _assertion(Decimal("10")).assertion_key,
        _assertion(Decimal("10.0")).assertion_key,
        _assertion(Decimal("10.00")).assertion_key,
    }
    assert len(keys) == 1


@pytest.mark.unit
def test_agreement_gate_commits_money_proposals_differing_only_in_scale():
    ep = [_Ep(uuid="money-scale-152", content="my savings balance is $10")]
    samples = [
        extract_typed_scalars_once(ep, _llm([_money_row(value=v, stated_span="my savings balance is $10")]))
        for v in ("10", "10.0", "10.00")
    ]
    assert len(samples) == 3 and all(len(s) == 1 for s in samples)

    decisions = gate_typed_scalars(samples)

    assert len(decisions) == 1
    assert decisions[0].committed is True, (
        f"committed={decisions[0].committed} reason={decisions[0].reason!r} "
        f"agreement={decisions[0].agreement} dist={decisions[0].distribution}"
    )
    assert decisions[0].agreement == 1.0
    assert decisions[0].proposal is not None
    assert isinstance(decisions[0].proposal.value, Decimal)


@pytest.mark.unit
def test_agreement_gate_control_genuinely_different_values_do_not_commit():
    ep = [_Ep(uuid="money-scatter-152", content="my savings balance is $10")]
    samples = [
        extract_typed_scalars_once(ep, _llm([_money_row(value=v, stated_span="my savings balance is $10")]))
        for v in ("10", "10.01", "10.02")
    ]
    assert len(samples) == 3 and all(len(s) == 1 for s in samples)

    decisions = gate_typed_scalars(samples)

    assert len(decisions) == 1
    assert decisions[0].committed is False
    assert decisions[0].agreement < 1.0
