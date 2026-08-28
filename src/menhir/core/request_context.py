"""Request-scoped caller context shared by API, MCP, and backend adapters.

This module owns only neutral context propagation. Transport-specific session
creation, telemetry, namespace policy, and trust decisions remain with their
adapters.
"""

from __future__ import annotations

from contextvars import ContextVar, Token

from menhir.domain.session import MemorySession

_request_session: ContextVar[MemorySession | None] = ContextVar(
    "menhir_request_session", default=None
)
_request_auth_mode: ContextVar[str] = ContextVar(
    "menhir_request_auth_mode", default="none"
)
_request_tier: ContextVar[str] = ContextVar("menhir_request_tier", default="")
_request_tool_allowlist: ContextVar[frozenset[str] | None] = ContextVar(
    "menhir_request_tool_allowlist", default=None
)


def bind_request_session(session: MemorySession) -> Token[MemorySession | None]:
    """Bind an already-resolved caller session to the current request context."""

    return _request_session.set(session)


def reset_request_session(token: Token[MemorySession | None]) -> None:
    """Restore the previous request-scoped caller session."""

    _request_session.reset(token)


def get_request_session() -> MemorySession | None:
    """Return the caller session bound to the current request, if any."""

    return _request_session.get()


def bind_request_auth_mode(mode: str) -> Token[str]:
    """Bind the current transport authentication mode."""

    return _request_auth_mode.set(mode)


def reset_request_auth_mode(token: Token[str]) -> None:
    """Restore the previous authentication mode."""

    _request_auth_mode.reset(token)


def get_request_auth_mode() -> str:
    """Return the authentication mode bound to the current request."""

    return _request_auth_mode.get()


def bind_request_tier(tier: str) -> Token[str]:
    """Bind the resolved authorization tier for the current request."""

    return _request_tier.set(tier)


def reset_request_tier(token: Token[str]) -> None:
    """Restore the previous authorization tier."""

    _request_tier.reset(token)


def get_request_tier() -> str:
    """Return the bound authorization tier, or an empty string when unbound."""

    return _request_tier.get()


def bind_request_tool_allowlist(
    tools: frozenset[str],
) -> Token[frozenset[str] | None]:
    """Bind the immutable client-policy tool allowlist for one request."""
    return _request_tool_allowlist.set(frozenset(tools))


def reset_request_tool_allowlist(token: Token[frozenset[str] | None]) -> None:
    _request_tool_allowlist.reset(token)


def get_request_tool_allowlist() -> frozenset[str] | None:
    """Return a policy allowlist, distinguishing unbound from deny-all."""
    return _request_tool_allowlist.get()
