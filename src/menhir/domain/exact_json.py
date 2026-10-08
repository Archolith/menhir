"""The one JSON encoder for values that may hold ``Decimal`` (typed money/number scalars).

``json.dumps`` raises on ``Decimal``; ``default=str`` turns it into a string and loses the type.
``dumps_exact`` writes each finite ``Decimal`` as an exact JSON number at its own scale
(``Decimal("12.50")`` -> ``12.50``) and is byte-identical to ``json.dumps(obj, **kwargs)`` when
no ``Decimal`` is present. Non-finite values use ``json.dumps``'s float form (``NaN``,
``Infinity``, ``-Infinity``).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from decimal import Decimal
from typing import Any
from uuid import uuid4

__all__ = ["dumps_exact"]


def _literal(value: Decimal) -> str:
    if value.is_finite():
        return format(value, "f")
    return json.dumps(float("nan") if value.is_nan() else float(value))


def dumps_exact(obj: Any, **kwargs: Any) -> str:
    """``json.dumps(obj, **kwargs)`` with ``Decimal`` values written as exact JSON numbers."""
    literals: dict[str, str] = {}
    prefix = f"__menhir_decimal_{uuid4().hex}_"

    def swap(value: Any) -> Any:
        if isinstance(value, Decimal):
            token = f"{prefix}{len(literals)}"
            literals[token] = _literal(value)
            return token
        if isinstance(value, Mapping):
            return {k: swap(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [swap(v) for v in value]
        return value

    text = json.dumps(swap(obj), **kwargs)
    for token, literal in literals.items():
        text = text.replace(f'"{token}"', literal)
    return text
