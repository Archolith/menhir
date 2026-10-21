"""View payloads must serialize typed Decimal values (money scalars) as exact JSON numbers."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from menhir.infrastructure.view_models import ScalarHistoryKind, _dumps_payload

pytestmark = pytest.mark.unit


def _entry(assertion_id: str, value_json, valid_at: str) -> dict:
    return {
        "assertion_id": assertion_id,
        "operation": "set",
        "value": str(value_json),
        "value_json": value_json,
        "valid_at": valid_at,
    }


def test_scalar_history_write_props_accepts_decimal_values() -> None:
    payload = {
        "attribute": "rent",
        "value_kind": "money",
        "unit": "usd",
        "entries": [
            _entry("a1", Decimal("1450.00"), "2023-01-01"),
            _entry("a2", Decimal("1523.10"), "2024-01-01"),
        ],
    }

    props = ScalarHistoryKind().write_props("user", "sh_x", payload)

    raw = props["view_payload"]
    assert '"value_json": 1450.00' in raw and '"value_json": 1523.10' in raw
    decoded = json.loads(raw, parse_float=Decimal)
    assert [e["value_json"] for e in decoded] == [Decimal("1450.00"), Decimal("1523.10")]


def test_dumps_payload_matches_json_dumps_without_decimals() -> None:
    obj = [{"value_json": 3.5, "nested": {"k": [1, "é", None, True]}, "t": (1, 2)}]

    assert _dumps_payload(obj) == json.dumps(obj, ensure_ascii=False)


def test_dumps_payload_does_not_rewrite_lookalike_strings() -> None:
    obj = {"quote": "__menhir_decimal_deadbeef_0", "amount": Decimal("-0.5")}

    out = json.loads(_dumps_payload(obj), parse_float=Decimal)

    assert out == {"quote": "__menhir_decimal_deadbeef_0", "amount": Decimal("-0.5")}


def test_dumps_payload_non_finite_decimal_uses_json_float_form() -> None:
    assert _dumps_payload([Decimal("NaN"), Decimal("Infinity")]) == json.dumps(
        [float("nan"), float("inf")]
    )
