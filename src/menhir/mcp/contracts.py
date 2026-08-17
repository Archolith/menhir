"""Shared MCP contract classes for tools and resources."""

from __future__ import annotations

import inspect
import json
import logging
import threading
import time
from abc import ABC, abstractmethod
from collections import deque
from functools import wraps
from typing import TYPE_CHECKING, Any, Awaitable

from menhir.core.backend_protocol import MemoryBackend
from menhir.infrastructure.telemetry import record_destructive_op, record_mcp_event
from menhir.mcp.service_access import (
    build_memory_backend,
    get_client_tool_allowlist,
    get_pinned_namespace,
    get_request_session,
    get_request_tier,
    request_uses_query_auth,
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
_query_add_memory_events: dict[str, deque[float]] = {}
_query_add_memory_lock = threading.Lock()


def _query_auth_rate_limit_key() -> str:
    session = get_request_session()
    if session is None:
        return "query-auth:anonymous"
    return session.client_id or session.session_id or session.user_id or "query-auth:anonymous"


def _consume_query_add_memory_budget(now: float | None = None) -> tuple[int, float]:
    current = time.time() if now is None else now
    key = _query_auth_rate_limit_key()
    cutoff = current - QUERY_AUTH_ADD_MEMORY_WINDOW_SECONDS
    with _query_add_memory_lock:
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

    def error_mapper(self, error_text: str) -> str:
        return render_json(
            {
                "ok": False,
                "resource": self.operation,
                "error": {"message": error_text},
            }
        )

    async def execute(self, *args: Any, **kwargs: Any) -> str:
        async def _run() -> str:
            # Query-string auth is a legacy compatibility exception for a bounded set
            # of tools, not a general MCP credential. Resources are refused outright:
            # routing them through QUERY_AUTH_ALLOWED_TOOLS would expand the exception
            # just because every resource currently has required_tier="readonly".
            if request_uses_query_auth():
                raise PermissionError(
                    f"query-string auth cannot read `{self.uri}`; use Authorization header for resources"
                )
            tier = get_request_tier()
            if tier and not _tier_allows(tier, self.required_tier):
                raise PermissionError(
                    f"Token tier '{tier}' cannot read `{self.uri}` (requires '{self.required_tier}')"
                )
            # MENHIR_CLIENT_TOOLS is intentionally not consulted here. Its values are
            # tool names and its catalog counterpart filters tools/list only; matching
            # those values against resource names or URI templates would silently deny
            # all resources for restricted clients. A resource ACL, if introduced, must
            # be a distinct policy/config surface with explicit resource semantics.
            payload = await self.build_payload(*args, **kwargs)
            return render_json(payload)

        return await track_mcp_call(
            kind=self.kind,
            operation=self.operation,
            payload=self.call_payload(*args, **kwargs),
            runner=_run,
            error_mapper=self.error_mapper,
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


class BaseTool:
    """Contract base for MCP tools."""

    name: str
    description: str
    response_kind = "text"
    required_tier: str = "agent"  # minimum tier required to invoke this tool

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

        async def _runner() -> str:
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
            tier = get_request_tier()
            if tier and not _tier_allows(tier, self.required_tier):
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
            return await self.endpoint(*args, **call_kwargs)

        result = await track_mcp_call(
            kind="tool",
            operation=self.operation,
            payload=self.call_payload(*args, **kwargs),
            runner=_runner,
            timeout=self.timeout_for(*args, **kwargs),
            error_mapper=error_mapper,
        )
        from menhir.core.backend_impl import drain_client_warnings
        warnings = drain_client_warnings()
        if warnings:
            warn_block = "\n".join(f"[background-error] {w}" for w in warnings)
            result = f"{result or ''}\n\n{warn_block}"
        return result or ""

    def register(self, mcp: FastMCP) -> None:
        target = self.endpoint

        @wraps(target)
        async def handler(*args: Any, **kwargs: Any) -> str:
            return await self.execute(*args, **kwargs)

        handler.__name__ = self.name
        handler.__qualname__ = self.name
        mcp.tool()(handler)


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
