"""MCP backend resolution for local runtime vs backend-first client mode."""

from __future__ import annotations

import logging
import threading
from contextvars import Token
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx

from menhir.config import MemorySettings
from menhir.core.backend_config import resolve_backend_auth_key
from menhir.core.backend_impl import BackendClient, RuntimeProvider
from menhir.core.backend_protocol import MemoryBackend
from menhir.core.request_context import (
    bind_request_auth_mode,
    bind_request_session as _bind_request_session_context,
    bind_request_tier,
    get_request_auth_mode,
    get_request_session,
    get_request_tier,
    reset_request_auth_mode,
    reset_request_session,
    reset_request_tier,
)
from menhir.domain.session import MemorySession, new_session

_client_session: MemorySession | None = None
_session_cache: dict[tuple[str, str | None, str, str], MemorySession] = {}
_session_cache_lock = threading.Lock()
logger = logging.getLogger(__name__)


def _normalized_backend_url(settings: MemorySettings | None = None) -> str:
    settings = settings or MemorySettings.from_env()
    return (settings.backend_url or "").strip().rstrip("/")


def backend_client_mode_enabled(settings: MemorySettings | None = None) -> bool:
    """Return True when MCP should run in backend-client mode."""

    return bool(_normalized_backend_url(settings))


def resolve_mcp_backend_url(
    settings: MemorySettings | None = None,
    explicit_url: str | None = None,
) -> str:
    """Resolve the MCP backend URL with documented priority.

    Priority:
    1. *explicit_url* (CLI/config argument)
    2. ``settings.backend_url`` (MENHIR_BACKEND_URL)
    3. ``http://<api_host>:<api_port>`` (default loopback)

    Trailing slashes are stripped.
    """
    settings = settings or MemorySettings.from_env()
    raw = explicit_url or settings.backend_url
    if raw:
        return raw.strip().rstrip("/")
    return f"http://{settings.api_host}:{settings.api_port}"


def resolve_mcp_backend_auth_key(settings: MemorySettings | None = None) -> str:
    """Return the bearer token for MCP backend-client requests.

    Priority:
    1. ``settings.agent_key`` (MENHIR_AGENT_KEY)
    2. ``settings.api_key`` (MENHIR_API_KEY — legacy)
    3. ``""`` — no auth / dev mode

    Never returns a non-empty string with leading/trailing whitespace.
    Whitespace-only keys are treated as absent so they do not block fallback.
    """
    return resolve_backend_auth_key(settings)


def redact_url_for_diagnostics(raw: str) -> str:
    """Redact userinfo from a URL for safe diagnostic display.

    ``http://user:pass@host:8099/path`` -> ``http://***:***@host:8099/path``
    """
    try:
        parsed = urlparse(raw)
        if parsed.username or parsed.password:
            netloc = parsed.hostname or ""
            if parsed.port:
                netloc = f"{netloc}:{parsed.port}"
            redacted = parsed._replace(netloc=f"***:***@{netloc}")
            return urlunparse(redacted)
        return raw
    except Exception:
        return raw


def build_mcp_backend_diagnostics(
    settings: MemorySettings | None = None,
) -> dict[str, Any]:
    """Build a redacted JSON-serializable diagnostics block for MCP backend-client mode.

    No secrets are included. Auth key presence is reported as booleans only.
    Whitespace-only keys are not counted as present.
    """
    settings = settings or MemorySettings.from_env()
    enabled = backend_client_mode_enabled(settings)
    url = resolve_mcp_backend_url(settings)
    agent_key = bool((settings.agent_key or "").strip())
    api_key = bool((settings.api_key or "").strip())
    will_send_auth = bool(resolve_mcp_backend_auth_key(settings))

    warnings: list[str] = []

    if enabled and not will_send_auth:
        warnings.append("No bearer key configured — backend requests will be unauthenticated.")

    return {
        "enabled": enabled,
        "backend_url": redact_url_for_diagnostics(url),
        "auth_header_will_be_sent": will_send_auth,
        "agent_key_present": agent_key,
        "api_key_present": api_key,
        "warnings": warnings,
    }


async def probe_backend_health(settings: MemorySettings | None = None, *, timeout_s: float = 5.0) -> dict[str, object]:
    """Probe the configured backend and return a compact health summary."""

    backend_url = _normalized_backend_url(settings)
    if not backend_url:
        return {"ok": False, "reason": "backend_url_not_configured", "url": ""}

    ready_url = f"{backend_url}/api/ready"
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        response = await client.get(ready_url)
        response.raise_for_status()
        payload = response.json() if response.content else {}
    return {
        "ok": True,
        "url": backend_url,
        "status": payload.get("status"),
        "startup_mode": payload.get("startup_mode"),
    }


def _cached_session_for(
    user_id: str,
    session_id: str | None = None,
    *,
    client_id: str = "",
    client_name: str = "",
) -> MemorySession:
    key = (user_id, session_id, client_id, client_name)
    with _session_cache_lock:
        session = _session_cache.get(key)
        if session is None:
            session = new_session(user_id, session_id=session_id, client_id=client_id, client_name=client_name)
            _session_cache[key] = session
    return session


def bind_request_session(
    user_id: str,
    session_id: str | None = None,
    *,
    client_id: str = "",
    client_name: str = "",
) -> Token[MemorySession | None]:
    """Bind a request-scoped MCP caller session for the current context.

    Touches session_registry (per-window) and client_registry (device-level).
    """
    session = _cached_session_for(user_id, session_id, client_id=client_id, client_name=client_name)
    try:
        from menhir.mcp.telemetry import telemetry_store
        # Per-session touch — each conversation/window tracks independently
        telemetry_store.touch_session(session.session_id, client_id, client_name or user_id)
        # Device-level touch — for list_clients / overall identity
        if client_id:
            telemetry_store.touch_client(client_id, client_name or user_id)
    except Exception:
        pass
    return _bind_request_session_context(session)


def get_pinned_namespace(settings: MemorySettings | None = None) -> str:
    """Return the namespace this client is pinned to, or "" if it is not pinned.

    Pinning is server-side config (MENHIR_CLIENT_NAMESPACES), keyed on the caller's
    client name. It exists because a caller cannot always be trusted to scope its
    own writes -- a game-chat bot driven by a small model will not reliably pass the
    right namespace argument. When a client is pinned, contracts.py FORCES the
    namespace on every tool call, so the caller's argument cannot override it.
    """

    session = get_request_session()
    if session is None:
        return ""
    client_name = (getattr(session, "client_name", "") or "").strip().lower()
    if not client_name:
        return ""
    settings = settings or MemorySettings.from_env()
    return (settings.client_namespaces or {}).get(client_name, "")


def get_client_tool_allowlist(settings: MemorySettings | None = None) -> frozenset[str]:
    """Return the tool allowlist this client is restricted to, or empty if none.

    The tool-surface analogue of :func:`get_pinned_namespace`. Server-side config
    (MENHIR_CLIENT_TOOLS), keyed on the caller's client name, names the exact set
    of tools a client may see and invoke. It exists for the same reason as the
    namespace pin: a small-model client cannot be trusted to navigate the full
    catalog, so it is handed a tiny purpose-built toolset instead.

    This policy is intentionally tool-only. MCP resources are not compared against
    these values because operators configure tool names, not resource names or URI
    templates; reusing this set for resources would deny every resource to a client
    with a non-empty tool allowlist. If resource ACLs are needed, they require a
    separate config surface with explicit resource matching semantics.

    An empty result (no request session, an unnamed caller, or a client with no
    configured entry) means "no restriction" -- the caller keeps the full,
    tier-filtered catalog, so default behavior is unchanged.
    """

    session = get_request_session()
    if session is None:
        return frozenset()
    client_name = (getattr(session, "client_name", "") or "").strip().lower()
    if not client_name:
        return frozenset()
    settings = settings or MemorySettings.from_env()
    return (settings.client_tools or {}).get(client_name, frozenset())


def request_uses_query_auth() -> bool:
    """Return True when the current request was authenticated via URL query token."""

    return get_request_auth_mode() == "query"


def bind_stdio_local_trust() -> Token[str]:
    """Bind operator tier for the local stdio MCP process (CT-002).

    The stdio server runs in-process as a trusted local agent. Its real security
    boundary is filesystem access to the SQLite stores (client_tokens.db, the
    memory DB) — a process that can read/write those can already do anything a
    tier gate would allow. Binding operator tier explicitly keeps operator tools
    callable over stdio and makes that trust decision visible, instead of relying
    on the implicit empty-tier bypass in ``BaseTool.execute`` (which was the
    undocumented status quo). HTTP requests always bind a real tier via the auth
    middleware, so this only affects the stdio transport.
    """

    return bind_request_tier("operator")


def build_memory_backend(settings: MemorySettings | None = None) -> MemoryBackend:
    """Return the active MCP backend implementation.

    Prefers the local RuntimeProvider when the runtime is initialized (server
    mode).  Only falls back to BackendClient when the runtime is not available
    (stdio MCP running out-of-process).
    """

    from menhir.core.runtime import _state

    built = _state.built
    session = _state.session
    if built is not None and session is not None:
        return RuntimeProvider(
            built,
            process_session=session,
            caller_session=get_request_session(),
        )

    settings = settings or MemorySettings.from_env()
    backend_url = _normalized_backend_url(settings)
    if backend_url:
        return BackendClient(backend_url, settings=settings)

    raise RuntimeError("menhir runtime is not ready and no backend URL configured")


def get_mcp_session(settings: MemorySettings | None = None) -> MemorySession:
    """Return the caller session for MCP-originated ingest operations."""

    request_session = get_request_session()
    if request_session is not None:
        return request_session

    from menhir.core.runtime import _state as _runtime_state

    if _runtime_state.session is not None:
        return _runtime_state.session

    settings = settings or MemorySettings.from_env()
    if not backend_client_mode_enabled(settings):
        raise RuntimeError("menhir runtime is not ready")

    global _client_session
    if _client_session is None or _client_session.user_id != settings.mcp_client_user_id:
        _client_session = _cached_session_for(
            settings.mcp_client_user_id,
            client_id=settings.mcp_client_id,
            client_name=settings.mcp_client_name or settings.mcp_client_user_id,
        )
        if settings.mcp_client_id:
            try:
                from menhir.mcp.telemetry import telemetry_store
                telemetry_store.touch_client(
                    settings.mcp_client_id,
                    settings.mcp_client_name or settings.mcp_client_user_id,
                )
            except Exception:
                pass
    return _client_session
