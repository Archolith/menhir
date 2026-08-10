"""Helper functions for environment-backed settings."""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def _parse_int(raw: str, *, env_var: str) -> int:
    try:
        return int(raw)
    except ValueError:
        raise ValueError(
            f"Environment variable {env_var}={raw!r} cannot be parsed as an integer"
        ) from None


def _parse_float(raw: str, *, env_var: str) -> float:
    try:
        return float(raw)
    except ValueError:
        raise ValueError(
            f"Environment variable {env_var}={raw!r} cannot be parsed as a float"
        ) from None


def _getenv(primary: str, *aliases: str, default: str) -> str:
    """Return the first set env var from primary then aliases, else default."""
    for key in (primary, *aliases):
        val = os.getenv(key)
        if val is not None:
            return val
    return default


def parse_client_tools(raw: str) -> dict[str, frozenset[str]]:
    """Parse MENHIR_CLIENT_TOOLS into {client_name: {allowed_tool, ...}}.

    Format: ``client-name=tool1|tool2|tool3,other=toolA|toolB``. Clients are
    separated by commas; the tool names for one client are separated by ``|``
    (a comma would collide with the client separator). Client names are matched
    case-insensitively (they arrive via the X-Menhir-Client-Name header); tool
    names are kept verbatim so they match the registered tool ids exactly.

    A client mapped to a non-empty set is restricted to exactly those tools:
    ``tools/list`` hides the rest and invocation of a hidden tool is refused.
    A client with no entry (the default for every client) is unrestricted, so
    existing behavior is preserved. Malformed entries are skipped rather than
    crashing startup; an entry whose tool list is empty after parsing is
    dropped (an empty allowlist would silently hide every tool).
    """
    mapping: dict[str, frozenset[str]] = {}
    for entry in (raw or "").split(","):
        entry = entry.strip()
        if not entry or "=" not in entry:
            continue
        client, _, tools_raw = entry.partition("=")
        client = client.strip().lower()
        tools = frozenset(t.strip() for t in tools_raw.split("|") if t.strip())
        if client and tools:
            mapping[client] = mapping.get(client, frozenset()) | tools
    return mapping

def parse_client_namespaces(raw: str) -> dict[str, str]:
    """Parse MENHIR_CLIENT_NAMESPACES into {client_name: namespace}.

    Format: ``client-name=namespace,other=ns2``. Client names are matched
    case-insensitively (they arrive via the X-Menhir-Client-Name header).
    Malformed entries are skipped rather than crashing startup.
    """
    mapping: dict[str, str] = {}
    for entry in (raw or "").split(","):
        entry = entry.strip()
        if not entry or "=" not in entry:
            continue
        client, _, namespace = entry.partition("=")
        client, namespace = client.strip().lower(), namespace.strip()
        if client and namespace:
            mapping[client] = namespace
    return mapping


_TRUTHY = ("true", "1", "yes")


def parse_bool_env(raw: str) -> bool:
    """Parse an env-var string into a bool using the one canonical truthy set.

    This is the single source of truth for boolean env parsing in menhir --
    every ``MENHIR_*_ENABLED``-style flag in this module goes through it, so
    a value like ``on`` either counts everywhere or nowhere (previously
    ``MENHIR_CLIENT_TOKENS_ENABLED=on`` disagreed: ``True`` via
    ``api.client_token_store.client_tokens_enabled()``'s own ad hoc set that
    included ``on``, ``False`` via this module's set that didn't -- see
    SSOT-07). ``on`` is intentionally not truthy: no flag in this codebase
    documents it as an accepted value, only ``1``/``true``/``yes``.
    """
    return raw.strip().lower() in _TRUTHY


def parse_csv_env(raw: str) -> tuple[str, ...]:
    """Parse a comma-delimited setting once at snapshot construction."""
    return tuple(item.strip() for item in raw.split(",") if item.strip())


# ---------------------------------------------------------------------------
# Loopback auth safety guard
# ---------------------------------------------------------------------------

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def is_loopback_host(host: str) -> bool:
    """Return True if *host* is a known loopback address."""
    return host.strip().lower() in _LOOPBACK_HOSTS


def validate_no_auth_bind_safety(
    *,
    host: str,
    auth_keys_present: bool,
    allow_insecure_remote_no_auth: bool = False,
    oauth_enabled: bool = False,
    client_tokens_enabled: bool = False,
) -> None:
    """Raise ``ValueError`` if no-key auth would bind to a non-loopback host.

    When no bearer keys are configured AND neither OAuth nor the per-client token
    tier is enabled, the server can only safely bind to loopback addresses.
    Non-loopback hosts require an authenticated mode (static keys, OAuth
    resource-server, or the enforced per-client token tier), or an explicit
    opt-in via ``MENHIR_ALLOW_INSECURE_REMOTE_NO_AUTH``.
    """
    if auth_keys_present:
        return

    if oauth_enabled:
        return

    if client_tokens_enabled:
        return

    if is_loopback_host(host):
        return

    if allow_insecure_remote_no_auth:
        logger.warning(
            "MENHIR_ALLOW_INSECURE_REMOTE_NO_AUTH is set — binding no-key server "
            "to %s.  This is unsafe outside isolated lab networks.",
            host,
        )
        return

    raise ValueError(
        f"Refusing to bind no-key/open-auth server to {host!r}. "
        f"Set MENHIR_API_KEY (or MENHIR_AGENT_KEY / MENHIR_OPERATOR_KEY / "
        f"MENHIR_READONLY_KEY) for auth, or bind to a loopback address "
        f"(127.0.0.1, localhost, ::1). "
        f"To explicitly allow remote no-auth, set "
        f"MENHIR_ALLOW_INSECURE_REMOTE_NO_AUTH=1 (unsafe)."
    )


def assert_bind_safe(settings: object, *, host: str | None = None) -> None:
    """SSOT bind-safety check: resolve the auth mode once, then enforce it.

    The single entry point for the bind guard. Both settings construction
    (``__post_init__``) and the CLI ``serve`` command call this, so the guard can
    never disagree with the mode the middleware actually enforces — a class of
    drift that previously caused OAuth-only remote binds to be wrongly refused
    (S-001) and one CLI path to omit the OAuth/client-token signals entirely.
    """
    from menhir.config.auth_mode import resolve_auth_mode

    mode = resolve_auth_mode(settings)
    validate_no_auth_bind_safety(
        host=host if host is not None else getattr(settings, "api_host", ""),
        auth_keys_present=mode.is_authenticated,
        allow_insecure_remote_no_auth=bool(
            getattr(settings, "allow_insecure_remote_no_auth", False)
        ),
    )
