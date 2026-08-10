"""OAuth configuration contracts and environment-backed construction."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _split_csv(raw: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _quote_header_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


_MISSING_SETTING = object()


def _get_setting(
    settings: object,
    attr: str,
    env_var: str,
    default: object,
    *aliases: str,
) -> object:
    """Read a snapshot value, with environment fallback for legacy objects.

    Once a settings object owns an attribute, even an empty value is
    authoritative. This keeps a running `MemorySettings` snapshot immutable
    while preserving compatibility with older lightweight settings objects.
    """

    value = getattr(settings, attr, _MISSING_SETTING)
    if value is not _MISSING_SETTING:
        return value
    for key in (env_var, *aliases):
        raw = os.getenv(key)
        if raw not in (None, ""):
            return raw
    return default


def _as_tuple(value: object, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    if value in (None, "", ()):
        return default
    if isinstance(value, str):
        parsed = _split_csv(value)
        return parsed or default
    if isinstance(value, (list, tuple, set)):
        parsed = tuple(str(item).strip() for item in value if str(item).strip())
        return parsed or default
    return default


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "1", "yes", "on")


@dataclass(frozen=True)
class OAuthConfig:
    """Configuration for validating OAuth access tokens on remote endpoints."""

    enabled: bool = False
    public_base_url: str = ""
    resource: str = ""
    authorization_servers: tuple[str, ...] = ()
    issuer: str = ""
    jwks_uri: str = ""
    audiences: tuple[str, ...] = ()
    scopes_supported: tuple[str, ...] = (
        "menhir:read",
        "menhir:write",
        "menhir:admin",
    )
    read_scopes: tuple[str, ...] = ("menhir:read",)
    write_scopes: tuple[str, ...] = ("menhir:write",)
    admin_scopes: tuple[str, ...] = ("menhir:admin",)
    jwks_cache_ttl_s: int = 300
    http_timeout_s: float = 5.0
    clock_skew_s: int = 60
    allowed_algorithms: tuple[str, ...] = ("RS256",)

    @property
    def metadata_url(self) -> str:
        base = self.public_base_url.rstrip("/")
        if not base:
            return "/.well-known/oauth-protected-resource"
        return f"{base}/.well-known/oauth-protected-resource"

    def challenge(
        self,
        *,
        error: str | None = None,
        description: str | None = None,
        scope: str | None = None,
    ) -> str:
        """Build the Bearer challenge used by MCP clients for discovery."""

        parts = [f'Bearer resource_metadata="{self.metadata_url}"']
        if error:
            parts.append(f'error="{_quote_header_value(error)}"')
        if description:
            parts.append(
                f'error_description="{_quote_header_value(description)}"'
            )
        if scope:
            parts.append(f'scope="{_quote_header_value(scope)}"')
        return ", ".join(parts)


def build_oauth_config(settings: object) -> OAuthConfig:
    """Build `OAuthConfig` from a settings snapshot or legacy settings object."""

    public_base_url = str(
        _get_setting(settings, "oauth_public_base_url", "MENHIR_PUBLIC_BASE_URL", "")
    ).rstrip("/")
    configured_resource = str(
        _get_setting(
            settings,
            "oauth_resource",
            "MENHIR_OAUTH_RESOURCE",
            "",
            "MENHIR_MCP_RESOURCE",
        )
    ).strip()
    resource = configured_resource or (
        f"{public_base_url}/mcp-http" if public_base_url else ""
    )
    audiences = _as_tuple(
        _get_setting(
            settings,
            "oauth_audiences",
            "MENHIR_OAUTH_AUDIENCE",
            (),
            "MENHIR_OAUTH_AUDIENCES",
        )
    )
    if not audiences and resource:
        audiences = (resource,)

    rs_enabled = _as_bool(
        _get_setting(settings, "oauth_enabled", "MENHIR_OAUTH_ENABLED", False)
    )
    issuer = str(
        _get_setting(settings, "oauth_issuer", "MENHIR_OAUTH_ISSUER", "")
    ).strip()
    jwks_uri = str(
        _get_setting(settings, "oauth_jwks_uri", "MENHIR_OAUTH_JWKS_URI", "")
    ).strip()
    authorization_servers = _as_tuple(
        _get_setting(
            settings,
            "oauth_authorization_servers",
            "MENHIR_AUTHORIZATION_SERVERS",
            (),
        )
    )

    as_enabled = _as_bool(
        _get_setting(settings, "oauth_as_enabled", "MENHIR_OAUTH_AS_ENABLED", False)
    )
    if as_enabled and public_base_url:
        if not issuer:
            issuer = public_base_url
        if not jwks_uri:
            jwks_uri = f"{public_base_url}/.well-known/jwks.json"
        if not authorization_servers:
            authorization_servers = (public_base_url,)

    return OAuthConfig(
        enabled=rs_enabled or as_enabled,
        public_base_url=public_base_url,
        resource=resource,
        authorization_servers=authorization_servers,
        issuer=issuer,
        jwks_uri=jwks_uri,
        audiences=audiences,
        scopes_supported=_as_tuple(
            _get_setting(
                settings,
                "oauth_scopes_supported",
                "MENHIR_OAUTH_SCOPES_SUPPORTED",
                (),
            ),
            OAuthConfig.scopes_supported,
        ),
        read_scopes=_as_tuple(
            _get_setting(
                settings,
                "oauth_read_scopes",
                "MENHIR_OAUTH_READ_SCOPES",
                (),
            ),
            OAuthConfig.read_scopes,
        ),
        write_scopes=_as_tuple(
            _get_setting(
                settings,
                "oauth_write_scopes",
                "MENHIR_OAUTH_WRITE_SCOPES",
                (),
            ),
            OAuthConfig.write_scopes,
        ),
        admin_scopes=_as_tuple(
            _get_setting(
                settings,
                "oauth_admin_scopes",
                "MENHIR_OAUTH_ADMIN_SCOPES",
                (),
            ),
            OAuthConfig.admin_scopes,
        ),
        jwks_cache_ttl_s=int(
            _get_setting(
                settings,
                "oauth_jwks_cache_ttl_s",
                "MENHIR_OAUTH_JWKS_CACHE_TTL_S",
                OAuthConfig.jwks_cache_ttl_s,
            )
        ),
        http_timeout_s=float(
            _get_setting(
                settings,
                "oauth_http_timeout_s",
                "MENHIR_OAUTH_HTTP_TIMEOUT_S",
                OAuthConfig.http_timeout_s,
            )
        ),
        clock_skew_s=int(
            _get_setting(
                settings,
                "oauth_clock_skew_s",
                "MENHIR_OAUTH_CLOCK_SKEW_S",
                OAuthConfig.clock_skew_s,
            )
        ),
        allowed_algorithms=_as_tuple(
            _get_setting(
                settings,
                "oauth_allowed_algorithms",
                "MENHIR_OAUTH_ALLOWED_ALGORITHMS",
                (),
            ),
            OAuthConfig.allowed_algorithms,
        ),
    )
