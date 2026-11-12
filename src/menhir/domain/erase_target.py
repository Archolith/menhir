"""Strict validation of an erase target.

An erase target must be a canonical lowercase uuid. Anything else (a short id such as ``"e"``, a
prefix, a substring) is refused before any graph lookup, PREPARE or sidecar purge, because the
downstream scrubs match by value and a short string would match unrelated content.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

INVALID_TARGET = "invalid_target"

_CANONICAL_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


def normalize_erase_target(value: object) -> str | None:
    """Return the stripped, lowercased canonical uuid, or ``None`` if ``value`` is not one."""
    if not isinstance(value, str):
        return None
    candidate = value.strip().lower()
    return candidate if _CANONICAL_UUID_RE.match(candidate) else None


def canonical_uuid_needles(values: Iterable[object]) -> list[str]:
    """Normalized, de-duplicated canonical uuids from ``values``; everything else is dropped."""
    out: list[str] = []
    for value in values:
        normalized = normalize_erase_target(value)
        if normalized and normalized not in out:
            out.append(normalized)
    return out
