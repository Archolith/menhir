"""Deterministic text of a counted View, for View selection by embedding.

A counted scalar_state View carries its attribute (`mattress_count`), the items it counted and the
actions seen per item. `count_view_text` renders them as one short line that a question can be
compared to by cosine. Pure: no I/O, same input -> same text.
"""

from __future__ import annotations

import json
from typing import Any


def _coerce(value: Any) -> Any:
    """A JSON string (as stored on the View) becomes its value; bad JSON or None becomes None."""
    if isinstance(value, (str, bytes)):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return None
    return value


def _names(value: Any) -> list[str]:
    value = _coerce(value)
    if value is None:
        return []
    if isinstance(value, dict):
        raw = list(value.keys())
    elif isinstance(value, (list, tuple, set, frozenset)):
        raw = list(value)
    else:
        return []
    out = [str(x).strip() for x in raw if x is not None]
    return [x for x in out if x]


def count_view_text(ss_attribute: Any, count_items: Any, count_actions: Any) -> str:
    """`<name>: <items or name>; <actions>` with the name taken from the attribute.

    The trailing `_count` or `_spend` is dropped and `_` becomes a space. Items keep their input
    order; for a dict of actions the keys in insertion order are used."""
    name = str(ss_attribute or "").strip()
    for suffix in ("_count", "_spend"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    name = name.replace("_", " ").strip()
    items = _names(count_items)
    actions = _names(count_actions)
    text = f"{name}: {', '.join(items) or name}; {', '.join(actions)}"
    return text.rstrip("; ").rstrip()


__all__ = ["count_view_text"]
