"""Ownership-at-load for object-addressed MCP tools (CF-33 step 4).

A NAMESPACED tool is bounded by its argument: the pin is injected into `namespace` and the
query filters on it. An OBJECT-addressed tool has no such argument to inject -- the caller names
a specific uuid -- so the boundary has to be checked where the object is loaded instead.

CF-64 established the shape on `delete_memory` and `flag_memory`. This module is that shape
factored out, because doing it per-tool is exactly how this cluster produced six findings: every
per-site fix was correct and every one exempted the next tool someone added.

**Two lookups, and the second is the whole point.** Refusing whenever the object is not found
IN the caller's namespace would also refuse when it is not in the graph AT ALL -- and absent is
not an ownership violation. `delete_memory` is the case that proves it: `graph_already_absent`
is how a merge leaves the node it absorbed, whose stored content must still be erasable. So
absent-everywhere proceeds, and only an object that demonstrably belongs to another silo is
refused.

**What this does not claim.** The load-bearing isolation boundary is `group_id` at the engine
layer; this is defense-in-depth at the transport boundary, the same standing as the `namespace`
node property it reads. It is also two round trips rather than one, which leaves a narrow window
where an object's namespace could change between them -- a single query returning the object's
namespace for comparison in Python would close both, and is the better long-term shape. It is
not done here because `MEMORY_RETURN_FIELDS` does not carry `namespace` and widening it reaches
far past this change.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Protocol

from menhir.core.tenancy import foreign_object_refusal as core_foreign_object_refusal


class ScopedLookup(Protocol):
    """A lookup that can be restricted to one silo.

    Each object family supplies its own: episodes and memory nodes through
    `fetch_memory_by_uuid`, artifacts and todos through their own repositories. The guard is
    the same; only the way you find the object differs.
    """

    def __call__(self, uuid: str, *, namespace: str | None = ...) -> Awaitable[Any]: ...


async def foreign_object_refusal(
    *,
    uuid: str,
    namespace: str,
    lookup: ScopedLookup | Callable[..., Awaitable[Any]],
    label: str,
) -> str | None:
    """Return a refusal message when *uuid* belongs to a different silo, else ``None``.

    ``None`` means proceed -- either the caller is unpinned and unscoped (isolation is opt-in),
    the object is theirs, or it does not exist anywhere.

    The tool-facing name for `core.tenancy.foreign_object_refusal`, which is the single
    implementation. It moved to `core` when the same guard was needed at the backend boundary,
    because `core` cannot import from `mcp` and a second copy of "does this object belong to
    this caller" is exactly the divergence this cluster keeps producing.

    Args:
        uuid: The object identifier the caller named.
        namespace: The caller's silo. Empty means no scoping was requested.
        lookup: ``await lookup(uuid, namespace=...)`` -> object or None.
        label: What to call the object in the refusal, e.g. "episode", "artifact".
    """
    return await core_foreign_object_refusal(
        uuid=uuid, namespace=namespace, lookup=lookup, label=label
    )
