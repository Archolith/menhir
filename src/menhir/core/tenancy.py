"""The tenant boundary, resolved and enforced below transport-specific code.

The invariant this module exists to hold:

    No caller holding a valid credential may escape its configured namespace by changing
    `client_name`, choosing another namespace, omitting namespace, switching between the REST
    and MCP surfaces, or addressing another tenant's object by UUID.

Every previous fix in this cluster enforced that at ONE transport and left the others open --
CF-16 at resources, CF-30 at named REST routes, CF-33 at MCP tools, CF-221 at the internal
backend dispatch. Each was correct. Each exempted whatever came next, because the boundary was
being drawn per-entry-point rather than under all of them.

This module lives in `core` precisely so it sits BELOW both transports. `core` does not import
from `mcp` or `api`, so a guard placed here cannot be bypassed by choosing a different surface:
REST handlers, the internal backend dispatch, and MCP tools all funnel into the same backend
methods, and the check runs there.

**Two lookups, and the second one is the point.** Refusing whenever an object is not found IN
the caller's namespace would also refuse when it is not in the graph AT ALL -- and absent is not
an ownership violation. `delete_memory` proves it has to work that way: `graph_already_absent`
is how a merge leaves the node it absorbed, whose stored content must still be erasable. So
absent-everywhere proceeds, and only an object that demonstrably belongs to another silo is
refused.

**What this does not claim.** `group_id` remains the load-bearing isolation boundary at the
engine layer; this is defense-in-depth at the service boundary, the same standing as the
`namespace` node property it reads. It is also two round trips, leaving a narrow window where an
object's namespace could change between them.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from menhir.config import MemorySettings

from .request_context import get_request_session


def pinned_namespace(settings: MemorySettings | None = None) -> str:
    """The namespace this caller is pinned to, or "" when it is not pinned.

    Pinning is server-side config (`MENHIR_CLIENT_NAMESPACES`) keyed on the caller's client name.
    It exists because a caller cannot always be trusted to scope its own writes -- a game-chat
    bot driven by a small model will not reliably pass the right namespace argument.

    This is the single authority for that resolution. `mcp.service_access.get_pinned_namespace`
    delegates here rather than reimplementing it: two copies of "which silo is this caller in"
    would agree until someone edited one of them.
    """
    session = get_request_session()
    if session is None:
        return ""
    client_name = (getattr(session, "client_name", "") or "").strip().lower()
    if not client_name:
        return ""
    settings = settings or MemorySettings.from_env()
    return (settings.client_namespaces or {}).get(client_name, "")


async def foreign_object_refusal(
    *,
    uuid: str,
    namespace: str,
    lookup: Callable[..., Awaitable[Any]],
    label: str,
) -> str | None:
    """Return a refusal message when *uuid* belongs to another silo, else ``None``.

    ``None`` means proceed -- the caller is unscoped (isolation is opt-in), the object is
    theirs, or it does not exist anywhere.

    Args:
        uuid: The object identifier the caller named.
        namespace: The caller's silo. Empty means no scoping was requested.
        lookup: ``await lookup(uuid, namespace=...)`` -> object or None.
        label: What to call the object in the refusal, e.g. "memory", "artifact".
    """
    ns = (namespace or "").strip()
    if not ns:
        return None
    if await lookup(uuid, namespace=ns) is not None:
        return None
    if await lookup(uuid) is None:
        # Absent everywhere. Not an ownership violation -- see the module docstring.
        return None
    return f"Refused: {label} {uuid} exists but is outside namespace {ns}."


async def require_own_object(
    *,
    uuid: str,
    lookup: Callable[..., Awaitable[Any]],
    label: str = "object",
    settings: MemorySettings | None = None,
) -> None:
    """Raise ``PermissionError`` when the PINNED caller named another silo's object.

    The backend-boundary form of the guard, and the difference from the MCP-tool form is where
    the namespace comes from. A tool takes it as an argument, because the pin is injected into
    that argument. A backend method has no such argument and must not grow one for this: it is
    reached from REST handlers and the internal dispatch as well, and any parameter they forget
    to pass becomes the next bypass. So the pin is resolved from the request context here,
    where no caller can decline to supply it.

    An unpinned deployment resolves to "" and this returns immediately, so behaviour is
    unchanged for anyone not using per-client namespaces.
    """
    ns = pinned_namespace(settings)
    if not ns:
        return
    refusal = await foreign_object_refusal(
        uuid=uuid, namespace=ns, lookup=lookup, label=label
    )
    if refusal:
        raise PermissionError(refusal)


def require_namespace_target(requested: str | None, *, action: str,
                             settings: MemorySettings | None = None) -> str:
    """Refuse a pinned caller that named a namespace other than its own, and return the target.

    **Why refuse here when the pin FORCES everywhere else.** Forcing is right when the namespace
    is a FILTER: the caller asked to read something, and narrowing what it sees to its own silo
    gives it a correct answer to a smaller question. It is wrong when the namespace is the
    TARGET of a mutation. Silently rewriting `reset namespace=B` into `reset namespace=A`
    destroys the caller's own data while it believes it acted on someone else's -- turning an
    attempted cross-tenant action into a successful self-inflicted one.

    So: filters force, targets refuse. `action` names the operation in the error, because the
    caller needs to know which of its requests was rejected.

    An unpinned caller gets its requested namespace back unchanged.
    """
    target = (requested or "").strip()
    pin = pinned_namespace(settings)
    if pin and target and target != pin:
        raise PermissionError(
            f"Refused: this client is pinned to namespace {pin!r} and cannot {action} "
            f"namespace {target!r}."
        )
    # A pinned caller that named nothing gets its own silo rather than a server-wide default.
    return target or pin


def resolve_namespace_filter(requested: str | None,
                             settings: MemorySettings | None = None) -> str | None:
    """The FILTER counterpart: the pin wins, silently, and an unpinned caller is untouched.

    Mirrors `api.routes_support._resolve_namespace` and `BaseTool._apply_pinned_namespace` so the
    three surfaces cannot disagree about what a pinned caller's read means.
    """
    pin = pinned_namespace(settings)
    if pin:
        return pin
    value = (requested or "").strip()
    return value or None
