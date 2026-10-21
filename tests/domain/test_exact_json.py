"""dumps_exact: Decimal as exact JSON numbers, byte-identical to json.dumps otherwise."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import pytest

from menhir.domain.exact_json import dumps_exact
from menhir.infrastructure.typed_assertion_repair_repository import _scalar_value_json

pytestmark = pytest.mark.unit

_PLAIN: list[Any] = [
    {"b": 1, "a": [1.5, "x", None, True], "c": {"é": "ü"}},
    [],
    "text",
    7,
    None,
    ("t", 2),
]


@pytest.mark.parametrize("obj", _PLAIN)
@pytest.mark.parametrize("kwargs", [{}, {"ensure_ascii": False}, {"sort_keys": True}])
def test_matches_json_dumps_without_decimals(obj: Any, kwargs: dict[str, Any]) -> None:
    assert dumps_exact(obj, **kwargs) == json.dumps(obj, **kwargs)


def test_decimals_keep_their_scale_at_any_depth() -> None:
    obj = {"price": Decimal("12.50"), "rows": [{"amount": Decimal("-0.010")}], "n": 3}

    out = dumps_exact(obj, sort_keys=True)

    assert out == '{"n": 3, "price": 12.50, "rows": [{"amount": -0.010}]}'
    assert json.loads(out, parse_float=Decimal)["price"] == Decimal("12.50")


@pytest.mark.parametrize(
    ("value", "literal"),
    [
        (Decimal("NaN"), "NaN"),
        (Decimal("sNaN"), "NaN"),
        (Decimal("Infinity"), "Infinity"),
        (Decimal("-Infinity"), "-Infinity"),
    ],
)
def test_non_finite_uses_the_json_float_form(value: Decimal, literal: str) -> None:
    assert dumps_exact([value]) == f"[{literal}]"


def _old_scalar_value_json(value: Any) -> str:
    """The pre-consolidation encoder, for equivalence on finite values."""
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_old_scalar_value_json(item) for item in value) + "]"
    return json.dumps(value, ensure_ascii=False)


@pytest.mark.parametrize(
    "value",
    [
        Decimal("12.50"),
        [Decimal("1.0"), Decimal("2.25")],
        [[Decimal("3")], "café", 4, None],
        "plain",
        42,
        1.5,
        True,
        None,
        [],
        {"k": "v"},
    ],
)
def test_typed_scalar_encoding_is_unchanged(value: Any) -> None:
    assert _scalar_value_json(value) == _old_scalar_value_json(value)
