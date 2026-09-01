"""Instance-local ViewKind registry resolution."""

from __future__ import annotations

from collections.abc import Mapping

from menhir.infrastructure.view_models import ViewKind


def resolve_view_kinds(
    builtins: Mapping[str, ViewKind],
    kinds: Mapping[str, ViewKind] | None,
) -> dict[str, ViewKind]:
    """Return a validated, instance-local registry without mutating either input."""
    resolved = dict(builtins if kinds is None else kinds)
    for registered_name, kind in resolved.items():
        if registered_name != kind.name:
            raise ValueError(
                f"ViewKind registry key {registered_name!r} does not match "
                f"kind.name {kind.name!r}"
            )
    return resolved
