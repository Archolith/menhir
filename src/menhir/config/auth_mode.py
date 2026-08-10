"""Single source of truth for Menhir's effective authentication mode."""

from __future__ import annotations

from enum import Enum

from .oauth import build_oauth_config


class AuthMode(str, Enum):
    """The one authentication mode a server operates in."""

    OAUTH = "oauth"
    CLIENT_TOKEN = "client-token"
    STATIC = "static"
    NONE = "none"

    @property
    def is_authenticated(self) -> bool:
        """Return whether this mode enforces a credential."""

        return self is not AuthMode.NONE


def auth_mode_from(
    *,
    oauth_enabled: bool,
    client_tokens_enabled: bool,
    static_keys_present: bool,
) -> AuthMode:
    """Resolve OAuth > client-token > static-key > no-auth precedence."""

    if oauth_enabled:
        return AuthMode.OAUTH
    if client_tokens_enabled:
        return AuthMode.CLIENT_TOKEN
    if static_keys_present:
        return AuthMode.STATIC
    return AuthMode.NONE


def static_keys_present(settings: object) -> bool:
    """Return whether any static bearer key is configured."""

    return bool(
        getattr(settings, "api_key", "")
        or getattr(settings, "operator_key", "")
        or getattr(settings, "agent_key", "")
        or getattr(settings, "readonly_key", "")
    )


def resolve_auth_mode(settings: object) -> AuthMode:
    """Resolve the effective mode from a settings snapshot or legacy object."""

    return auth_mode_from(
        oauth_enabled=build_oauth_config(settings).enabled,
        client_tokens_enabled=bool(
            getattr(settings, "client_tokens_enabled", False)
        ),
        static_keys_present=static_keys_present(settings),
    )
