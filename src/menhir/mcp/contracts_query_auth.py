"""Query-string auth tool allowlist: lazy computation with a caching proxy."""

from __future__ import annotations

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
