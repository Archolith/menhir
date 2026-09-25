"""Request vocabulary for the auth middleware: path classification and identity headers.

Extracted verbatim from ``auth.py`` (which re-exports the public names) to keep the
facade module small. Nothing here imports the middleware, so this module is a safe
leaf dependency for the auth mixins.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

type ASGIApp = Callable
type Scope = dict
type Receive = Callable
type Send = Callable

_EXEMPT_PATHS = frozenset({"/api/health", "/api/ready"})
_EXEMPT_PATH_PREFIXES = frozenset({"/explorer/static"})
_ADMIN_PATH_PREFIX = "/api/admin/"
# The only admin capability an unauthenticated loopback caller may bootstrap.
# Every other admin action (e.g. revoke) requires a real operator credential so
# it always carries provenance.
_ADMIN_MINT_PATH = "/api/admin/clients"
# Sentinel identity bound for an unauthenticated loopback bootstrap mint. The
# mint route keys the atomic empty-store guard (CT-003) on this value.
LOOPBACK_BOOTSTRAP_ID = "loopback-bootstrap"

# Reverse-proxy forwarding headers. Their presence means the request traversed a
# proxy, so the peer socket address is the proxy's — not the real client's. Used
# to deny the loopback bootstrap window behind a same-host proxy (CT-001).
_PROXY_FORWARDING_HEADERS = (b"x-forwarded-for", b"x-real-ip", b"forwarded")

_SENSITIVE_SINGLETON_HEADERS = frozenset(
    {
        b"authorization",
        *(f"x-menhir-{suffix}".encode() for suffix in (
            "user-id",
            "session-id",
            "client-id",
            "client-name",
            "namespace",
        )),
    }
)


def _duplicate_sensitive_headers(headers: Sequence[tuple[bytes, bytes]]) -> list[str]:
    """Return duplicated auth/identity header names.

    Different HTTP stacks disagree about whether the first or last duplicate wins.
    Rejecting ambiguity prevents a proxy and Menhir from authenticating or attributing
    the same request differently.
    """

    counts: dict[bytes, int] = {}
    for raw_name, _ in headers:
        name = raw_name.lower()
        if name in _SENSITIVE_SINGLETON_HEADERS:
            counts[name] = counts.get(name, 0) + 1
    return sorted(name.decode("ascii", errors="replace") for name, count in counts.items() if count > 1)


def _identity_header(headers: dict[bytes, bytes], suffix: bytes) -> str:
    """Read a caller identity header.

    ``x-menhir-*`` is the only accepted spelling. A deprecated ``x-yawn-*`` alias was
    honoured while older client configurations still sent it; none do, and carrying a second
    accepted spelling for caller identity means two ways to assert who you are. Callers must
    apply the same trust gate to the result that they would to any identity header -- this
    helper does no authorization.
    """
    return headers.get(b"x-menhir-" + suffix, b"").decode("latin-1").strip()


def _is_mcp_path(path: str) -> bool:
    """Return True when *path* is an MCP endpoint (SSE root, sub-path, or Streamable HTTP)."""
    return path == "/mcp" or path.startswith("/mcp/") or path == "/mcp-http"
