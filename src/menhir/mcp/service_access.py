"""MCP backend resolution for local runtime vs backend-first client mode."""

from __future__ import annotations

import logging
import os
import threading
from collections import OrderedDict
from contextvars import Token
from typing import Any
from urllib.parse import urlparse

import httpx

from menhir.config import MemorySettings, redact_uri_for_display
from menhir.config.settings_helpers import is_loopback_host
from menhir.core.backend_config import resolve_backend_auth_key
from menhir.core.backend_impl import BackendClient, RuntimeProvider
from menhir.core.backend_protocol import MemoryBackend
from menhir.core.tenancy import pinned_namespace as core_pinned_namespace
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
#: Upper bound on memoized MCP caller sessions (CF-89). ``MemorySession`` is a frozen
#: dataclass and nothing mutates a cached one, so eviction cannot lose a write.
#:
#: It is not free, though. When the cached key carries ``session_id=None``, this cache is what
#: gives that caller a STABLE synthetic id: ``new_session`` mints a fresh ``uuid4()`` and a new
#: ``started_at`` on a miss. So evicting such an entry splits one logical conversation into two
#: in session_registry and telemetry. Nothing is corrupted and no gate is affected -- the id is
#: not an authorization input -- but the bound is set well above the number of concurrent
#: callers a deployment sees so this stays a theoretical cost rather than a routine one.
_SESSION_CACHE_MAX = 256
_session_cache: "OrderedDict[tuple[str, str | None, str, str], MemorySession]" = OrderedDict()
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

    PURE RESOLUTION -- this does not enforce the CF-42 scheme rule, deliberately. Its only
    production caller is ``build_mcp_backend_diagnostics``, whose whole job is to REPORT a bad
    configuration; raising here made ``menhir diagnostics`` crash on precisely the misconfiguration
    it exists to surface. Enforcement lives on ``_normalized_backend_url``, the path that actually
    hands the URL to a ``BackendClient`` along with a bearer key.
    """
    settings = settings or MemorySettings.from_env()
    raw = explicit_url or settings.backend_url
    if raw:
        return raw.strip().rstrip("/")
    return f"http://{settings.api_host}:{settings.api_port}"


#: Opt out of the non-loopback HTTPS requirement. Named like its sibling so an operator searching
#: for one finds the other.
_ALLOW_INSECURE_BACKEND_ENV = "MENHIR_ALLOW_INSECURE_BACKEND_URL"


def _require_secure_backend_url(url: str) -> str:
    """Refuse an ``http://`` backend URL that is not loopback (CF-42)."""
    if not url:
        return url
    try:
        parsed = urlparse(url)
    except ValueError:
        return url
    if parsed.scheme != "http":
        return url
    host = (parsed.hostname or "").strip().lower()
    if is_loopback_host(host):
        return url

    if os.getenv(_ALLOW_INSECURE_BACKEND_ENV, "").strip().lower() in ("1", "true", "yes"):
        logger.warning(
            "%s is set: sending the backend bearer key to %s over plaintext HTTP.",
            _ALLOW_INSECURE_BACKEND_ENV,
            host,
        )
        return url

    raise ValueError(
        f"Refusing a plaintext backend URL for non-loopback host {host!r}: every request to it "
        f"carries an Authorization bearer key. Use https://, point at a loopback address, or set "
        f"{_ALLOW_INSECURE_BACKEND_ENV}=1 to accept the risk explicitly."
    )


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
    """Reduce a URL to scheme, authority and path for safe diagnostic display.

    ``http://user:pass@host:8099/path`` -> ``http://host:8099/path``
    ``https://backend.example/p?token=s`` -> ``https://backend.example/p``

    CF-97: this used to strip userinfo and nothing else, so a credential in a query string --
    the shape ``MENHIR_BACKEND_URL`` actually takes when an operator uses a token -- printed
    verbatim in ``menhir diagnostics``. It also failed OPEN, returning the raw string on any
    parse error. It now delegates to the one shared reducer, which fails closed.
    """
    return redact_uri_for_display(raw)


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
            if len(_session_cache) > _SESSION_CACHE_MAX:
                _session_cache.popitem(last=False)
        else:
            _session_cache.move_to_end(key)
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

    Delegates to `core.tenancy.pinned_namespace`, which is the single authority. The backend
    boundary needs this same resolution and `core` cannot import from `mcp`, so the logic moved
    down rather than being copied -- two answers to "which silo is this caller in" would agree
    until someone edited one of them.
    """

    return core_pinned_namespace(settings)


def client_restrictions_configured(settings: MemorySettings | None = None) -> bool:
    """Whether this deployment configures any per-client policy at all.

    The switch for both CF-32's identity refusal and CF-83's mint refusal. A deployment with no
    restrictions has no policy to evade, so neither check applies and behaviour is unchanged.
    """
    settings = settings or MemorySettings.from_env()
    return bool(settings.client_namespaces) or bool(settings.client_tools)


def declared_client_names(settings: MemorySettings | None = None) -> frozenset[str]:
    """Every client name this deployment recognizes, from all three registries.

    One authority, consulted by the identity refusal (CF-32) and the mint refusal (CF-83). Two
    copies of "what counts as a declared name" would be the CF-47 failure mode: they would agree
    until someone edited one of them.
    """
    settings = settings or MemorySettings.from_env()
    return frozenset(
        set(settings.client_namespaces or {})
        | set(settings.client_tools or {})
        | set(settings.known_clients or frozenset())
    )


#: Auth modes under which the caller SUPPLIES its own `client_name` rather than having it derived
#: from a validated credential. Under these, the name is a claim, not an identity.
#:
#: `oauth` and `client_token` are excluded because there the name comes from the credential the
#: server itself issued, so a caller cannot rename itself into a different policy.
_SELF_DECLARED_IDENTITY_MODES = frozenset({"header", "query", "admin"})


def require_trusted_client_identity(settings: MemorySettings | None = None) -> None:
    """Refuse a self-named caller when per-client restrictions are configured (CF-32).

    Under static-key auth the caller supplies `client_name` via header or MCP metadata, and both
    `get_pinned_namespace` and `get_client_tool_allowlist` key on that value with an unknown name
    meaning "unrestricted". So a holder of the shared static key chose which policy applied to it
    by naming itself something not in the config -- the restriction was opt-in by the party it
    restricts.

    The refusal is deliberately ALL-OR-NOTHING on the deployment, not per-client: if any client
    restriction is configured, every self-named caller must be a configured name. Refusing only
    callers who name a *restricted* client would leave the evasion completely intact, since the
    evasion is precisely to claim a name that is not restricted.

    **Operational consequence, stated plainly because it will surprise someone:** configuring a
    pin for one client means every other static-key client must now be registered in
    `MENHIR_CLIENT_NAMESPACES` or `MENHIR_CLIENT_TOOLS`. An unregistered client that worked
    yesterday will be refused. That is the chosen behaviour -- fail loud, so a misconfiguration is
    visible the first time it happens rather than silently running unrestricted -- but it is a
    breaking change for any deployment that mixes pinned and unpinned static-key clients.

    Deployments with no client restrictions at all are untouched: there is no policy to evade, so
    there is nothing to refuse.

    A name counts as recognized if it appears in ANY of the three registries. `MENHIR_KNOWN_CLIENTS`
    exists for exactly this check: "recognized" and "restricted" are different facts, and without a
    third list, registering an ordinary client such as `claude-code` would have forced a namespace
    pin on it purely as a side effect of making it nameable.
    """

    settings = settings or MemorySettings.from_env()
    if not client_restrictions_configured(settings):
        return

    if get_request_auth_mode() not in _SELF_DECLARED_IDENTITY_MODES:
        return

    session = get_request_session()
    if session is None:
        return

    client_name = (getattr(session, "client_name", "") or "").strip().lower()
    if not client_name:
        raise PermissionError(
            "This deployment configures per-client restrictions, so a caller must identify "
            "itself. Set the client name (x-menhir-client-name, or MCP client_name metadata) "
            "to a name listed in MENHIR_KNOWN_CLIENTS, MENHIR_CLIENT_NAMESPACES or "
            "MENHIR_CLIENT_TOOLS."
        )

    if client_name not in declared_client_names(settings):
        raise PermissionError(
            f"Unknown client name {client_name!r}. This deployment configures per-client "
            "restrictions, so an unrecognized name is refused rather than treated as "
            "unrestricted. Add it to MENHIR_KNOWN_CLIENTS to recognize it without restricting "
            "it, or to MENHIR_CLIENT_NAMESPACES / MENHIR_CLIENT_TOOLS to restrict it."
        )


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
        # CF-42 is enforced HERE, at the one point the URL is handed to a client that will attach
        # a bearer key. Not on `_normalized_backend_url`, which also backs the
        # `backend_client_mode_enabled` predicate, and not on `resolve_mcp_backend_url`, whose only
        # caller is diagnostics -- a reporting surface must describe a bad configuration, not die on
        # it. Guarding a predicate or a report instead of the credential path is how a security
        # check ends up breaking everything except the thing it was meant to stop.
        return BackendClient(_require_secure_backend_url(backend_url), settings=settings)

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
