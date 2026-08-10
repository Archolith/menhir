"""Memory scope identity for self/non-self tolerance (belief-layer.md SelfToleranceGate).

A retrieved memory belongs to a place in the codebase: a repo, a project, a branch, a
namespace. A query is asked in a place too. When those disagree, the memory is "non-self"
for this query — an old-branch memory, another repo's memory, the same filename in a
different project — and must not enter the current-truth context. The ScopeWarden
(domain/warden.py) is the guard; this module is its pure conflict check.

This ``MemoryScope`` is identity scope (where the memory lives) and is DISTINCT from
``NodeScope`` (SESSION/PERSISTENT/CANDIDATE), which is the lifecycle/review tier. A memory
has both: a lifecycle tier and an identity scope.
"""

from __future__ import annotations

from dataclasses import dataclass

# The scope dimensions, in the order conflicts are reported. Each is a hard self/non-self
# axis: set on both sides and differing => wrong scope.
SCOPE_DIMENSIONS: tuple[str, ...] = ("repo", "project", "branch", "namespace")


@dataclass(frozen=True)
class MemoryScope:
    """Where a memory (or a query) lives. Any dimension may be unspecified (None)."""

    repo: str | None = None
    project: str | None = None
    branch: str | None = None
    namespace: str | None = None


def scope_conflict(query: MemoryScope, candidate: MemoryScope) -> tuple[bool, tuple[str, ...]]:
    """Return (is_conflict, conflicting_dimensions).

    A dimension conflicts only when it is set on BOTH sides and the values differ — an
    unspecified dimension never conflicts (absence is not non-self; it is "unknown",
    handled permissively so we never refuse on missing metadata). This is the self/non-self
    test: wrong repo/project/branch/namespace = non-self for the query."""
    conflicting: list[str] = []
    for dim in SCOPE_DIMENSIONS:
        q = getattr(query, dim)
        c = getattr(candidate, dim)
        if q and c and q != c:
            conflicting.append(dim)
    return (bool(conflicting), tuple(conflicting))
