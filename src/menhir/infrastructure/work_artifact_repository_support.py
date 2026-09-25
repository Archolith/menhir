"""Shared helpers for the WorkArtifactRepository facade split.

Holds the neo4j availability guard and the read-side namespace filter so the
facade and its mixin modules share one definition without circular imports.
"""

from __future__ import annotations


try:
    from neo4j.exceptions import ConstraintError as _Neo4jConstraintError
except ModuleNotFoundError:  # pragma: no cover - import guard
    _Neo4jConstraintError = ()  # type: ignore[assignment]


def _safe_namespace_filter(namespace: str | None) -> str | None:
    """The READ counterpart of `_safe_namespace`.

    `_safe_namespace` is for WRITES, where an unspecified namespace must still land somewhere,
    so it substitutes the default. A read must not do that: substituting the default would turn
    "caller did not ask to be scoped" into "show only the default silo", silently hiding data
    from every existing unscoped caller. Isolation is opt-in, so absent means no filter.
    """
    value = (namespace or "").strip()
    return value or None
