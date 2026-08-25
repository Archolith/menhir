"""Shared MCP contract classes for tools and resources."""

from __future__ import annotations

import inspect
import json
import logging
import threading
import time
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Mapping
from functools import wraps
from typing import TYPE_CHECKING, Any, Awaitable

from menhir.core.backend_protocol import MemoryBackend
from menhir.infrastructure.telemetry import record_destructive_op, record_mcp_event
from menhir.mcp.service_access import (
    build_memory_backend,
    get_client_tool_allowlist,
    get_pinned_namespace,
    oauth_tool_scope_denial,
    get_request_session,
    get_request_tier,
    request_uses_query_auth,
    require_trusted_client_identity,
)
from menhir.mcp.telemetry import track_mcp_call

if TYPE_CHECKING:
    from fastmcp import FastMCP

logger = logging.getLogger(__name__)


def _json_default(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def render_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True, default=_json_default)


# Compute query-auth allowlist dynamically at first use to avoid circular imports
_QUERY_AUTH_ALLOWED_TOOLS_CACHE: frozenset[str] | None = None


def _compute_query_auth_allowed_tools() -> frozenset[str]:
    """Compute the set of tools allowed with query-string auth.

    Policy: readonly tools (required_tier == "readonly") + add_memory (temporary,
    for header-less connectors; has its own rate budget).

    Cached after first computation to avoid repeated import overhead.
    """
    global _QUERY_AUTH_ALLOWED_TOOLS_CACHE
    if _QUERY_AUTH_ALLOWED_TOOLS_CACHE is not None:
        return _QUERY_AUTH_ALLOWED_TOOLS_CACHE

    try:
        from menhir.mcp.tools import ALL_TOOLS
        readonly_tools = {
            tool_cls.name
            for tool_cls in ALL_TOOLS
            if hasattr(tool_cls, "name") and getattr(tool_cls, "required_tier", "agent") == "readonly"
        }
    except (ImportError, AttributeError, Exception):
        # Fallback if tools can't be imported yet (e.g., during initial load)
        # or if the import structure changes. This ensures the system stays operational
        # even if the auto-computation fails; the manual allowlist is in the docstring.
        readonly_tools = set()

    result = frozenset(readonly_tools | {"add_memory"})
    _QUERY_AUTH_ALLOWED_TOOLS_CACHE = result
    return result


def _get_query_auth_allowed_tools() -> frozenset[str]:
    """Get the cached query auth allowlist, computing it on first call."""
    return _compute_query_auth_allowed_tools()


# Property-like access for backward compatibility
class _QueryAuthAllowlistProxy:
    """Proxy that computes the allowlist on first access and caches it."""
    def __contains__(self, item: str) -> bool:
        return item in _compute_query_auth_allowed_tools()

    def __iter__(self):
        return iter(_compute_query_auth_allowed_tools())

    def __repr__(self):
        return repr(_compute_query_auth_allowed_tools())

    def __eq__(self, other):
        return _compute_query_auth_allowed_tools() == other


QUERY_AUTH_ALLOWED_TOOLS = _QueryAuthAllowlistProxy()
QUERY_AUTH_ADD_MEMORY_LIMIT = 10
QUERY_AUTH_ADD_MEMORY_WINDOW_SECONDS = 600.0
#: Max keys EXAMINED per call by the expired-key sweep (CF-89). A round-robin cursor keeps the
#: per-call examination cost fixed no matter how large the dict has grown, and the advancing
#: cursor means stale keys keep coming back around to be removed.
#:
#: Stated precisely, because the sweep still snapshots the key list on each call and that part is
#: O(len(dict)): what is fixed is the number of buckets pruned and deleted, not the whole call.
#: The dict is nonetheless self-draining, which is the property that matters -- a call adds at
#: most one key and examines 32, so eviction always outruns arrival and the size plateaus at
#: roughly "distinct keys seen within one window" before draining back down.
_QUERY_AUTH_SWEEP_BUDGET = 32
_query_add_memory_events: dict[str, deque[float]] = {}
_query_add_memory_lock = threading.Lock()
_query_add_memory_sweep_cursor = 0


def _query_auth_rate_limit_key() -> str:
    session = get_request_session()
    if session is None:
        return "query-auth:anonymous"
    return session.client_id or session.session_id or session.user_id or "query-auth:anonymous"


def _sweep_query_add_memory_keys(current_key: str, cutoff: float) -> None:
    """Remove rate-limit buckets whose window has fully expired (CF-89).

    Only deletes a bucket when pruning against *cutoff* empties it — every timestamp it
    holds is ``<= cutoff``, which is exactly the same state as a fresh deque. Removing a
    fully-expired key is therefore safe, while leaving every bucket that still holds a live
    timestamp (so a caller can never gain a free reset — that would be a rate-limit bypass).

    Work is bounded per call: a round-robin cursor examines at most
    ``_QUERY_AUTH_SWEEP_BUDGET`` keys each sweep, so cost is constant regardless of how the
    dict has grown, and the advancing cursor guarantees every stale key is eventually
    revisited. The caller's own key is never evicted during the call using it.
    """
    global _query_add_memory_sweep_cursor
    keys = list(_query_add_memory_events.keys())
    n = len(keys)
    if n == 0:
        return
    idx = _query_add_memory_sweep_cursor % n
    examined = 0
    while examined < _QUERY_AUTH_SWEEP_BUDGET:
        k = keys[idx % n]
        if k != current_key:
            bucket = _query_add_memory_events.get(k)
            if bucket is not None:
                while bucket and bucket[0] <= cutoff:
                    bucket.popleft()
                if not bucket:
                    del _query_add_memory_events[k]
        idx += 1
        examined += 1
    _query_add_memory_sweep_cursor = idx % n


def _consume_query_add_memory_budget(now: float | None = None) -> tuple[int, float]:
    current = time.time() if now is None else now
    key = _query_auth_rate_limit_key()
    cutoff = current - QUERY_AUTH_ADD_MEMORY_WINDOW_SECONDS
    with _query_add_memory_lock:
        _sweep_query_add_memory_keys(key, cutoff)
        bucket = _query_add_memory_events.get(key)
        if bucket is None:
            bucket = deque()
            _query_add_memory_events[key] = bucket
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        if len(bucket) >= QUERY_AUTH_ADD_MEMORY_LIMIT:
            retry_after = max(1.0, QUERY_AUTH_ADD_MEMORY_WINDOW_SECONDS - (current - bucket[0]))
            return len(bucket), retry_after
        bucket.append(current)
        return len(bucket), 0.0


class ToolScope:
    """How a tool relates to tenant data, declared rather than inferred (CF-33).

    The namespace pin used to reach a tool only when `inspect.signature(self.endpoint)` happened
    to contain `namespace`. That is not a policy applied to callers; it is a policy applied to
    whichever endpoints happen to name a parameter. Adding a tool silently removed it from the
    pin's reach with no error and no log line, which is why this cluster has been patched
    per-site four times -- CF-16, CF-157, CF-64, CF-30 -- each correct, each leaving the next
    hole intact, and CF-30's own remediation finding a fifth inside the surface it had just fixed.

    Declaring the scope does not by itself close a hole. What it does is make an unconsidered
    tool a STARTUP FAILURE instead of a silent gap, and reduce "41 tools that are global by
    accident" to a list of roughly nine that a human can audit in a minute.
    """

    #: Takes a `namespace` argument; the pin is injected into it.
    NAMESPACED = "namespaced"
    #: Addressed by uuid. The pin cannot be injected as an argument, so tenancy must be checked
    #: at load -- the two-lookup pattern CF-64 established: refuse only when the object exists
    #: AND belongs to another silo, so genuinely-absent objects still report "not found".
    OBJECT = "object"
    #: Operational state that is genuinely not tenant-scoped (scheduler control, client admin).
    #: Kept deliberately small and reviewable.
    GLOBAL = "global"

    ALL = frozenset({NAMESPACED, OBJECT, GLOBAL})


_TIER_RANK: dict[str, int] = {"readonly": 0, "agent": 1, "operator": 2}


def _tier_allows(current: str, required: str) -> bool:
    """Return True when *current* tier satisfies the *required* tier."""
    return _TIER_RANK.get(current, -1) >= _TIER_RANK.get(required, 0)


def _try_log_query_auth_usage(tool_name: str) -> None:
    """Log query-auth tool invocation for data-driven removal (best-effort, non-blocking)."""
    try:
        record_mcp_event(
            kind="background",
            operation="query_auth_usage",
            payload={"tool": tool_name},
            success=True,
        )
    except Exception:
        pass  # Telemetry failures must never disrupt the caller


def _try_record_destructive_op_mcp(tool_name: str, tier: str) -> None:
    """Record operator-tier MCP tool invocation for audit log (best-effort, non-blocking)."""
    try:
        session = get_request_session()
        user_id = session.user_id if session else ""
        session_id = session.session_id if session else ""
        record_destructive_op(
            surface="mcp",
            name=tool_name,
            tier=tier,
            session_id=session_id,
            user_id=user_id,
        )
    except Exception:
        pass  # Telemetry failures must never disrupt the caller


def _declares_object_key(params: "Mapping[str, inspect.Parameter]") -> bool:
    """Whether an endpoint is actually addressed by an object identifier.

    OBJECT means "the pin cannot be injected as an argument because the caller names a specific
    object". A tool declaring OBJECT while naming no object is not making that claim -- it is
    the third row of CF-33's census, where genuinely-global tools and tenant-scoped tools that
    simply never got a `namespace` argument look identical from outside. Nine tools sat there,
    and CF-216, CF-217 and the four conflict tools all came out of it.

    The original check caught NAMESPACED-without-`namespace` and GLOBAL-with-`namespace` but not
    this, so the one declaration that meant "unexamined" was the one that stayed silent.
    """
    return any(
        "uuid" in name or name == "id" or name.endswith("_id")
        for name in params
        if name != "self"
    )


def assert_tool_scopes_declared(tool_classes: "list[type] | tuple[type, ...]") -> None:
    """Refuse to start when any tool has not declared its tenancy scope (CF-33).

    This is the load-bearing half of the ToolScope work. The enum alone documents; this is what
    makes an omission impossible to ship. Every finding in this cluster is an instance of one
    thing -- a tool was added and nobody decided how it relates to tenant data -- and that was
    invisible precisely because the consequence was silence.

    Also verifies the declaration matches the signature, because a wrong declaration is worse
    than none: a tool marked NAMESPACED whose endpoint has no `namespace` parameter would read
    as pinned in the audit list while the pin cannot actually reach it.

    Raises at import/registration time rather than logging, so a mistake stops a deploy instead
    of reaching production as a quiet gap.
    """

    undeclared: list[str] = []
    invalid: list[str] = []
    mismatched: list[str] = []

    for tool_cls in tool_classes:
        name = getattr(tool_cls, "name", tool_cls.__name__)
        scope = getattr(tool_cls, "scope", None)
        if scope is None:
            undeclared.append(name)
            continue
        if scope not in ToolScope.ALL:
            invalid.append(f"{name}={scope!r}")
            continue
        params = inspect.signature(tool_cls.endpoint).parameters
        declares_namespace = "namespace" in params
        if scope == ToolScope.NAMESPACED and not declares_namespace:
            mismatched.append(f"{name}: declared NAMESPACED but the endpoint takes no `namespace`")
        elif scope == ToolScope.GLOBAL and declares_namespace:
            mismatched.append(f"{name}: declared GLOBAL but the endpoint takes a `namespace`")
        elif scope == ToolScope.OBJECT and not _declares_object_key(params):
            mismatched.append(
                f"{name}: declared OBJECT but the endpoint takes no object identifier "
                "-- OBJECT means addressed by uuid, so a tool with neither a `namespace` nor "
                "an id is either tenant-scoped and missing its argument, or genuinely GLOBAL"
            )

    problems: list[str] = []
    if undeclared:
        problems.append(
            "tools with no `scope` declared: "
            + ", ".join(sorted(undeclared))
            + " -- set scope to one of "
            + ", ".join(sorted(ToolScope.ALL))
            + " (see ToolScope; NAMESPACED and OBJECT are tenant-scoped, GLOBAL is not)"
        )
    if invalid:
        problems.append("tools with an unrecognized `scope`: " + ", ".join(sorted(invalid)))
    if mismatched:
        problems.append("tools whose `scope` contradicts their signature: " + "; ".join(sorted(mismatched)))

    if problems:
        raise RuntimeError("MCP tool scope declarations are incomplete. " + " | ".join(problems))


class BaseJsonResource(ABC):
    """Contract-enforcing base for JSON MCP resources."""

    uri: str
    name: str
    description: str
    mime_type = "application/json"

    #: Minimum tier required to read this resource. "readonly" is the floor tier, so this
    #: is permissive by default and exists so an individual resource can raise its own bar
    #: (and so an unrecognised tier string is refused -- see _tier_allows).
    required_tier: str = "readonly"

    @property
    def kind(self) -> str:
        return "resource_template" if "{" in self.uri else "resource"

    @property
    def operation(self) -> str:
        return self.uri

    def get_backend(self) -> MemoryBackend:
        return build_memory_backend()

    def pinned_namespace(self) -> str | None:
        """Return the server-side namespace pin for this resource read, if any.

        Resource endpoint signatures expose URI-template arguments only, so the
        tool path's signature-aware ``_apply_pinned_namespace`` cannot protect
        them. Content-reading resources must pass this value into the backend read
        explicitly; an unpinned client returns ``None`` and keeps legacy behavior.
        """
        return get_pinned_namespace() or None

    def error_mapper(self, error_text: str) -> str:
        return render_json(
            {
                "ok": False,
                "resource": self.operation,
                "error": {"message": error_text},
            }
        )

    async def execute(self, *args: Any, **kwargs: Any) -> str:
        telemetry_effective_payload: dict[str, Any] | None = None

        async def _run() -> str:
            nonlocal telemetry_effective_payload
            # Query-string auth is a legacy compatibility exception for the tool
            # surface. QUERY_AUTH_ALLOWED_TOOLS is built from ALL_TOOLS only, so
            # resources have no established query-auth compatibility contract;
            # refuse them outright instead of extending or overloading that policy.
            if request_uses_query_auth():
                raise PermissionError(
                    f"query-string auth cannot read `{self.uri}`; use Authorization header for resources"
                )
            tier = get_request_tier()
            if not tier:
                raise PermissionError(
                    f"No request tier is bound; refusing to read `{self.uri}`. "
                    "Every caller must bind one -- HTTP via the auth middleware, stdio via "
                    "bind_stdio_local_trust()."
                )
            if not _tier_allows(tier, self.required_tier):
                raise PermissionError(
                    f"Token tier '{tier}' cannot read `{self.uri}` (requires '{self.required_tier}')"
                )
            # MENHIR_CLIENT_TOOLS is intentionally not consulted here. Its values are
            # tool names and its catalog counterpart filters tools/list only; matching
            # those values against resource names or URI templates would silently deny
            # all resources for restricted clients. A resource ACL, if introduced, must
            # be a distinct policy/config surface with explicit resource semantics.
            telemetry_effective_payload = self.call_payload(*args, **kwargs)
            payload = await self.build_payload(*args, **kwargs)
            return render_json(payload)

        return await track_mcp_call(
            kind=self.kind,
            operation=self.operation,
            payload=self.call_payload(*args, **kwargs),
            runner=_run,
            error_mapper=self.error_mapper,
            effective_payload=lambda: telemetry_effective_payload,
        )

    def call_payload(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        if kwargs:
            return dict(kwargs)
        if not args:
            return {}
        return {"args": list(args)}

    @abstractmethod
    async def build_payload(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """Return the JSON payload to render for this resource."""

    @abstractmethod
    def endpoint(self, *args: Any, **kwargs: Any) -> Awaitable[dict[str, Any]]:
        """Typed signature anchor used for MCP registration."""

    def register(self, mcp: FastMCP) -> None:
        target = self.endpoint

        @wraps(target)
        async def handler(*args: Any, **kwargs: Any) -> str:
            return await self.execute(*args, **kwargs)

        mcp.resource(
            self.uri,
            name=self.name,
            description=self.description,
            mime_type=self.mime_type,
        )(handler)


#: Mapping from required_tier to the exact OAuth scope set a tool must declare. The
#: metadata contract keeps tier and scope coherent so the advertised securitySchemes
#: cannot drift away from what invocation authorization actually enforces.
_TIER_OAUTH_SCOPES: dict[str, tuple[str, ...]] = {
    "readonly": ("menhir:read",),
    "agent": ("menhir:write",),
    "operator": ("menhir:admin",),
}

_SAFETY_HINT_FIELDS: tuple[str, ...] = ("read_only_hint", "destructive_hint", "open_world_hint")


def validate_tool_metadata(tool_classes: "list[type] | tuple[type, ...]") -> None:
    """Refuse to start when any tool has incomplete or incoherent ChatGPT metadata.

    Complements :func:`assert_tool_scopes_declared`: tenancy is checked there, the
    client-facing contract here. Every field below reaches the model choosing a tool
    (title, description, safety hints) or the connector's authorization layer
    (oauth_scopes), so an omission is a silent downgrade exactly like an undeclared
    scope was -- and gets the same treatment: a loud startup failure.

    Raises RuntimeError aggregating every problem across every tool rather than
    failing on the first one, so one bad tool does not hide the rest.
    """

    missing_fields: list[str] = []
    invalid_fields: list[str] = []
    scope_mismatches: list[str] = []

    for tool_cls in tool_classes:
        name = getattr(tool_cls, "name", tool_cls.__name__)

        for field in ("title", "description"):
            value = getattr(tool_cls, field, None)
            if not isinstance(value, str) or not value.strip():
                missing_fields.append(f"{name}.{field}")

        for field in _SAFETY_HINT_FIELDS:
            value = getattr(tool_cls, field, None)
            if not isinstance(value, bool):
                invalid_fields.append(f"{name}.{field}={value!r} (must be bool)")

        scopes = getattr(tool_cls, "oauth_scopes", None)
        if not isinstance(scopes, tuple) or not scopes or not all(isinstance(s, str) and s for s in scopes):
            invalid_fields.append(f"{name}.oauth_scopes={scopes!r} (must be a non-empty tuple of strings)")
            continue

        required_tier = getattr(tool_cls, "required_tier", None)
        expected_scopes = _TIER_OAUTH_SCOPES.get(required_tier)
        if expected_scopes is not None and tuple(scopes) != expected_scopes:
            scope_mismatches.append(
                f"{name}: required_tier={required_tier!r} requires oauth_scopes="
                f"{list(expected_scopes)} but declares {list(scopes)}"
            )

    problems: list[str] = []
    if missing_fields:
        problems.append("tools missing required text metadata: " + ", ".join(sorted(set(missing_fields))))
    if invalid_fields:
        problems.append("tools with invalid metadata fields: " + "; ".join(sorted(invalid_fields)))
    if scope_mismatches:
        problems.append("tools whose oauth_scopes contradict their required_tier: " + "; ".join(scope_mismatches))

    if problems:
        raise RuntimeError("MCP tool metadata contract violated. " + " | ".join(problems))


class BaseTool:
    """Contract base for MCP tools."""

    name: str
    description: str

    #: Human-facing display name surfaced to clients (e.g. ChatGPT tool UI).
    title: str

    #: Exact OAuth scopes the connector must grant to invoke this tool. Must match
    #: required_tier per _TIER_OAUTH_SCOPES; enforced by validate_tool_metadata.
    oauth_scopes: tuple[str, ...]

    #: MCP ToolAnnotations safety hints -- declared per tool, never defaulted, so a
    #: new tool cannot ship without a human deciding what each hint claims.
    read_only_hint: bool
    destructive_hint: bool
    open_world_hint: bool

    response_kind = "text"
    required_tier: str = "agent"  # minimum tier required to invoke this tool

    #: How this tool relates to tenant data. See :class:`ToolScope`.
    #:
    #: Deliberately declared with NO default that grants reach. `None` means "nobody has
    #: decided", and `assert_tool_scopes_declared` refuses to start on it -- so the failure mode
    #: for a newly added tool is a loud startup error rather than a tool silently outside the
    #: pin's reach, which is how every finding in this cluster came to exist.
    scope: str | None = None

    def get_backend(self) -> MemoryBackend:
        return build_memory_backend()

    @property
    def operation(self) -> str:
        return self.name

    def error_mapper(self, error_text: str) -> str | None:
        return None

    def call_payload(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        if kwargs:
            return dict(kwargs)
        if not args:
            return {}
        return {"args": list(args)}

    def timeout_for(self, *args: Any, **kwargs: Any) -> int:
        return 120

    def endpoint(self, *args: Any, **kwargs: Any) -> Awaitable[str]:
        """Typed MCP signature plus tool implementation."""
        raise NotImplementedError(f"{type(self).__name__} must implement endpoint()")

    def _accepts_namespace(self) -> bool:
        """True when this tool's endpoint takes a `namespace` parameter."""
        cached = getattr(type(self), "_namespace_param_cached", None)
        if cached is None:
            try:
                cached = "namespace" in inspect.signature(self.endpoint).parameters
            except (TypeError, ValueError):
                cached = False
            type(self)._namespace_param_cached = cached
        return cached

    def _apply_pinned_namespace(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        """Force a pinned client's namespace, overriding whatever it passed.

        Server-side config (MENHIR_CLIENT_NAMESPACES) wins over the caller's own
        argument -- that is the point. A client pinned to a namespace cannot escape
        it, whether by passing another namespace or by omitting the argument
        entirely (a small model does both). Unpinned clients are untouched, so
        default behavior is unchanged.
        """
        pinned = get_pinned_namespace()
        if not pinned or not self._accepts_namespace():
            return kwargs
        requested = str(kwargs.get("namespace") or "").strip()
        if requested and requested != pinned:
            logger.warning(
                "namespace pin: client requested namespace=%r for `%s`; forcing %r",
                requested,
                self.name,
                pinned,
            )
        return {**kwargs, "namespace": pinned}

    async def execute(self, *args: Any, **kwargs: Any) -> str:
        error_mapper = None
        if type(self).error_mapper is not BaseTool.error_mapper:
            error_mapper = self.error_mapper
        telemetry_effective_payload: dict[str, Any] | None = None

        async def _runner() -> str:
            nonlocal telemetry_effective_payload
            if request_uses_query_auth() and self.name not in QUERY_AUTH_ALLOWED_TOOLS:
                raise PermissionError(
                    f"query-string auth cannot invoke `{self.name}`; use Authorization header for write/admin tools"
                )
            if request_uses_query_auth():
                # Log query-auth usage for data-driven removal decision
                _try_log_query_auth_usage(self.name)
                if self.name == "add_memory":
                    count, retry_after = _consume_query_add_memory_budget()
                    if retry_after > 0:
                        raise PermissionError(
                            "query-string auth rate limit exceeded for `add_memory`; "
                            f"retry in about {int(retry_after)}s (limit {QUERY_AUTH_ADD_MEMORY_LIMIT} per "
                            f"{int(QUERY_AUTH_ADD_MEMORY_WINDOW_SECONDS)}s)"
                        )
            # CF-32: identity BEFORE policy. Both gates below key on `client_name`, and under
            # static-key auth the caller supplies that value itself -- so without this check a
            # holder of the shared key selected its own pin and its own allowlist by naming
            # itself something unconfigured, and an unknown name meant unrestricted.
            require_trusted_client_identity()

            # CF-34: FAIL CLOSED ON AN ABSENT TIER.
            #
            # The old form conjoined a truthiness check with the allows check, so an unbound
            # tier short-circuited to a PASS. (Described rather than quoted: the guard test
            # greps this function's source, so quoting the defect re-trips it.) The safe
            # default for an authorization check is to deny when it cannot determine the subject,
            # and this one admitted. It was never network-reachable -- HTTP binds a tier in the auth
            # middleware -- but it made the gate conditional on the transport having done its job,
            # which is the shape that turns a future refactor moving a call off that path into a
            # silent authorization hole rather than a crash.
            #
            # The empty tier used to be a DOCUMENTED supported state for local stdio. It is not any
            # more: `bind_stdio_local_trust()` (mcp/server.py:63) binds operator explicitly for that
            # process, which is the same trust decision made visibly instead of implicitly. Owner
            # ruling 2026-08-22: absent tier becomes denial, and every legitimate in-process path
            # binds one deliberately.
            tier = get_request_tier()
            if not tier:
                raise PermissionError(
                    f"No request tier is bound; refusing to invoke `{self.name}`. "
                    "Every caller must bind one -- HTTP via the auth middleware, stdio via "
                    "bind_stdio_local_trust()."
                )
            if not _tier_allows(tier, self.required_tier):
                declared_oauth_scopes = getattr(self, "oauth_scopes", ())
                oauth_denial = (
                    oauth_tool_scope_denial(
                        tool_name=self.name,
                        minimum_scope=declared_oauth_scopes[0],
                    )
                    if declared_oauth_scopes
                    else None
                )
                if oauth_denial is not None:
                    raise oauth_denial
                raise PermissionError(
                    f"Token tier '{tier}' cannot invoke `{self.name}` (requires '{self.required_tier}')"
                )
            # Per-client tool allowlist (MENHIR_CLIENT_TOOLS): defense in depth for
            # the list_tools filter. A restricted client that calls a tool outside
            # its allowlist -- whether it guessed the name or ignored tools/list --
            # is refused. Empty allowlist = unrestricted, so unnamed/unconfigured
            # callers are untouched.
            allowlist = get_client_tool_allowlist()
            if allowlist and self.name not in allowlist:
                raise PermissionError(
                    f"Client is not permitted to invoke `{self.name}` "
                    f"(restricted by MENHIR_CLIENT_TOOLS allowlist)"
                )
            # Record destructive ops (operator tier) for audit
            if self.required_tier == "operator":
                _try_record_destructive_op_mcp(self.name, tier)
            call_kwargs = self._apply_pinned_namespace(kwargs)
            telemetry_effective_payload = self.call_payload(*args, **call_kwargs)
            return await self.endpoint(*args, **call_kwargs)

        result = await track_mcp_call(
            kind="tool",
            operation=self.operation,
            payload=self.call_payload(*args, **kwargs),
            runner=_runner,
            timeout=self.timeout_for(*args, **kwargs),
            error_mapper=error_mapper,
            effective_payload=lambda: telemetry_effective_payload,
        )
        from menhir.core.backend_impl import drain_client_warnings
        warnings = drain_client_warnings()
        if warnings and isinstance(result, str):
            warn_block = "\n".join(f"[background-error] {w}" for w in warnings)
            result = f"{result or ''}\n\n{warn_block}"
        elif warnings:
            logger.warning(
                "Background client warnings omitted from non-text MCP result for %s",
                self.name,
            )
        return result or ""

    def registered_description(self) -> str:
        """The description handed to FastMCP at registration.

        The curated class attribute leads, because it is the one line written to be read by a
        model choosing a tool. The endpoint docstring follows when there is one, since it
        carries the argument documentation the class attribute does not. Before this, neither
        reached the client: `mcp.tool()` was called with no `description=`, and 36 of the 54
        endpoints have no docstring for `@wraps` to copy.
        """
        curated = (self.description or "").strip()
        detail = (self.endpoint.__doc__ or "").strip()
        if detail and detail != curated:
            return f"{curated}\n\n{detail}" if curated else detail
        return curated

    def register(self, mcp: FastMCP) -> None:
        target = self.endpoint

        @wraps(target)
        async def handler(*args: Any, **kwargs: Any) -> str:
            return await self.execute(*args, **kwargs)

        handler.__name__ = self.name
        handler.__qualname__ = self.name
        from mcp.types import ToolAnnotations

        tool_options: dict[str, Any] = dict(
            title=self.title,
            description=self.registered_description(),
            annotations=ToolAnnotations(
                title=self.title,
                readOnlyHint=self.read_only_hint,
                destructiveHint=self.destructive_hint,
                openWorldHint=self.open_world_hint,
            ),
            meta={
                "securitySchemes": [
                    {"type": "oauth2", "scopes": list(self.oauth_scopes)},
                ],
            },
        )
        # Menhir runs on the MCP SDK's FastMCP implementation, while some
        # descriptor-only tests use the standalone fastmcp package. Disable
        # inferred output schemas through the option supported by each runtime
        # so a protocol-native CallToolResult is passed through unchanged.
        tool_parameters = inspect.signature(mcp.tool).parameters
        if "structured_output" in tool_parameters:
            tool_options["structured_output"] = False
        elif "output_schema" in tool_parameters:
            tool_options["output_schema"] = None
        mcp.tool(**tool_options)(handler)


class BaseTextTool(BaseTool):
    """Base class for prose/text-returning tools."""

    response_kind = "text"


class BaseJsonTool(BaseTool):
    """Base class for tools that intentionally return JSON strings."""

    response_kind = "json"

    def render_json(self, payload: dict[str, Any]) -> str:
        return render_json(payload)

    def render_recall_json(self, payload: dict[str, Any]) -> str:
        """Render a recall payload after stamping a ratable ``recall_id`` token.

        Use this on the *success* path of recall tools so the agent can later call
        ``rate_recall`` to report whether the result was useful. Best-effort — the
        receipt never blocks the response (see ``menhir.mcp.feedback``).
        """
        from menhir.mcp.feedback import attach_recall_receipt

        attach_recall_receipt(self.operation, payload)
        return render_json(payload)

    def error_mapper(self, error_text: str) -> str:
        return self.render_json(
            {
                "ok": False,
                "tool": self.operation,
                "error": {"message": error_text},
            }
        )
