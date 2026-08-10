"""OAuth 2.1 resource-server helpers for remote MCP endpoints.

Menhir does not act as an authorization server. It validates access tokens
issued by an external IdP and maps token scopes onto the existing Menhir
request tiers.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from menhir.api import jose_provider
from menhir.config.oauth import (
    OAuthConfig,
    _as_bool,
    _as_tuple,
    _get_setting,
    build_oauth_config,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _as_scope_set(raw: object) -> set[str]:
    if raw is None:
        return set()
    if isinstance(raw, str):
        return {part for part in raw.split() if part}
    if isinstance(raw, (list, tuple, set)):
        return {str(part).strip() for part in raw if str(part).strip()}
    return set()


@dataclass(frozen=True)
class OAuthPrincipal:
    """Validated OAuth caller identity bound to a Menhir request."""

    subject: str
    client_id: str = ""
    client_name: str = ""
    scopes: frozenset[str] = field(default_factory=frozenset)
    tier: str = "readonly"
    claims: dict[str, Any] = field(default_factory=dict)


class OAuthAuthenticationError(Exception):
    """Authentication/authorization failure that can be rendered as a Bearer challenge."""

    def __init__(self, error: str, description: str, *, status_code: int = 401, scope: str | None = None) -> None:
        super().__init__(description)
        self.error = error
        self.description = description
        self.status_code = status_code
        self.scope = scope


# ---------------------------------------------------------------------------
# Scope/tier mapping
# ---------------------------------------------------------------------------


def extract_scopes(claims: dict[str, Any]) -> frozenset[str]:
    """Collect scopes from common OAuth/OIDC claim shapes."""
    scopes: set[str] = set()
    scopes |= _as_scope_set(claims.get("scope"))
    scopes |= _as_scope_set(claims.get("scp"))
    scopes |= _as_scope_set(claims.get("permissions"))
    return frozenset(scopes)


def tier_from_scopes(scopes: set[str] | frozenset[str], config: OAuthConfig) -> str | None:
    """Map OAuth scopes to the existing Menhir authorization tiers."""
    scope_set = set(scopes)
    if scope_set.intersection(config.admin_scopes):
        return "operator"
    if scope_set.intersection(config.write_scopes):
        return "agent"
    if scope_set.intersection(config.read_scopes):
        return "readonly"
    return None


# ---------------------------------------------------------------------------
# Token verifier
# ---------------------------------------------------------------------------


def _unverified_jwt_kid(token: str) -> str | None:
    """Return the ``kid`` from the JWT header WITHOUT verifying the signature.

    Used only to decide whether a decode failure could plausibly be fixed by a
    JWKS key-rotation refresh. Never trusted for authorization.
    """
    try:
        header_segment = token.split(".")[0]
        padding = "=" * (-len(header_segment) % 4)
        decoded = base64.urlsafe_b64decode(header_segment + padding)
        header = json.loads(decoded)
        kid = header.get("kid")
        return str(kid) if kid else None
    except Exception:
        return None


def _derive_subject(claims: dict[str, Any]) -> str:
    """Return a stable principal for the token.

    Human tokens carry ``sub``. Client-credentials tokens usually omit it, so
    fall back to the verified client identity. If neither is present, reject —
    never collapse distinct callers onto one synthetic identity.
    """
    sub = claims.get("sub")
    if sub:
        return str(sub)
    client = claims.get("client_id") or claims.get("azp")
    if client:
        return f"client:{client}"
    raise OAuthAuthenticationError(
        "invalid_token", "Access token has no subject or client identity"
    )


class OAuthTokenVerifier:
    """Validate JWT access tokens against an external IdP JWKS."""

    # Minimum seconds between attacker-triggerable forced JWKS refreshes.
    _FORCED_REFRESH_MIN_INTERVAL_S = 30.0

    def __init__(self, config: OAuthConfig) -> None:
        self.config = config
        self._jwks: Any | None = None
        self._jwks_expires_at = 0.0
        self._jwks_lock = asyncio.Lock()
        self._last_forced_refresh = 0.0
        self._algorithms = list(config.allowed_algorithms) or ["RS256"]

    async def verify_access_token(self, token: str) -> OAuthPrincipal:
        if not token:
            raise OAuthAuthenticationError("invalid_token", "Missing bearer token")
        if not self.config.enabled:
            raise OAuthAuthenticationError("invalid_request", "OAuth is not enabled")
        if not self.config.issuer:
            raise OAuthAuthenticationError("server_error", "OAuth issuer is not configured")
        if not self.config.jwks_uri:
            raise OAuthAuthenticationError("server_error", "OAuth JWKS URI is not configured")
        if not self.config.audiences:
            raise OAuthAuthenticationError("server_error", "OAuth audience/resource is not configured")

        claims = await self._decode_with_cached_jwks(token)
        self._validate_claims(claims)
        scopes = extract_scopes(claims)
        tier = tier_from_scopes(scopes, self.config)
        if tier is None:
            raise OAuthAuthenticationError(
                "insufficient_scope",
                "Access token does not include a Menhir scope",
                status_code=403,
                scope=" ".join(self.config.scopes_supported),
            )

        return OAuthPrincipal(
            subject=_derive_subject(claims),
            client_id=str(claims.get("client_id") or claims.get("azp") or ""),
            client_name=str(claims.get("client_name") or claims.get("azp") or "oauth-client"),
            scopes=scopes,
            tier=tier,
            claims=dict(claims),
        )

    def _decode_and_validate(self, token: str, key_set: Any) -> dict[str, Any]:
        # The JOSE library is confined to jose_provider; the algorithm allowlist
        # (T9 alg pin) and clock-skew leeway are enforced there.
        return jose_provider.verify_jwt(
            token, key_set, self._algorithms, self.config.clock_skew_s
        )

    def _cached_kid_present(self, kid: str | None) -> bool:
        """True if *kid* is already in the cached JWKS (no rotation needed)."""
        if self._jwks is None or kid is None:
            return False
        return jose_provider.jwks_has_kid(self._jwks, kid)

    def _forced_refresh_allowed(self) -> bool:
        """Rate-limit: True only if enough time passed since the last forced refresh."""
        return (
            time.monotonic() - self._last_forced_refresh
            >= self._FORCED_REFRESH_MIN_INTERVAL_S
        )

    async def _decode_with_cached_jwks(self, token: str) -> dict[str, Any]:
        key_set = await self._load_jwks()
        try:
            return self._decode_and_validate(token, key_set)
        except OAuthAuthenticationError:
            raise
        except Exception as first_exc:
            # Only a genuine key-rotation miss justifies bypassing the JWKS cache.
            # A malformed / expired / wrong-audience token must NOT trigger a
            # network refetch, or an unauthenticated caller can amplify junk
            # tokens into JWKS load against the IdP (DoS). We force-refresh only
            # when the token's kid is absent from the cached key set, and even
            # then no more than once per _FORCED_REFRESH_MIN_INTERVAL_S.
            kid = _unverified_jwt_kid(token)
            if self._cached_kid_present(kid) or not self._forced_refresh_allowed():
                raise OAuthAuthenticationError(
                    "invalid_token",
                    "Access token signature or claims are invalid",
                ) from first_exc
            self._last_forced_refresh = time.monotonic()
            key_set = await self._load_jwks(force_refresh=True)
            try:
                return self._decode_and_validate(token, key_set)
            except OAuthAuthenticationError:
                raise
            except Exception as exc:
                raise OAuthAuthenticationError(
                    "invalid_token",
                    "Access token signature or claims are invalid",
                ) from exc

    async def _load_jwks(self, *, force_refresh: bool = False) -> Any:
        now = time.monotonic()
        if not force_refresh and self._jwks is not None and now < self._jwks_expires_at:
            return self._jwks

        async with self._jwks_lock:
            now = time.monotonic()
            if not force_refresh and self._jwks is not None and now < self._jwks_expires_at:
                return self._jwks
            try:
                async with httpx.AsyncClient(timeout=self.config.http_timeout_s) as client:
                    response = await client.get(self.config.jwks_uri)
                    response.raise_for_status()
                    payload = response.json()
            except OAuthAuthenticationError:
                raise
            except Exception as exc:
                raise OAuthAuthenticationError("server_error", "Unable to fetch OAuth JWKS") from exc
            if not isinstance(payload, dict) or not isinstance(payload.get("keys"), list):
                raise OAuthAuthenticationError("server_error", "OAuth JWKS response is malformed")
            self._jwks = jose_provider.parse_jwks(payload)
            self._jwks_expires_at = time.monotonic() + max(1, self.config.jwks_cache_ttl_s)
            return self._jwks

    def _validate_claims(self, claims: dict[str, Any]) -> None:
        if claims.get("iss") != self.config.issuer:
            raise OAuthAuthenticationError("invalid_token", "Access token issuer is not trusted")
        if "exp" not in claims:
            raise OAuthAuthenticationError("invalid_token", "Access token is missing exp")
        if not _resource_matches(claims, self.config.audiences):
            raise OAuthAuthenticationError("invalid_token", "Access token audience/resource does not match Menhir")


def _claim_values(raw_value: object) -> set[str]:
    if isinstance(raw_value, str):
        return {raw_value}
    if isinstance(raw_value, (list, tuple, set)):
        return {str(item) for item in raw_value}
    return set()


def _resource_matches(claims: dict[str, Any], accepted: tuple[str, ...]) -> bool:
    """Return True when either aud or resource binds the token to Menhir.

    MCP deployments commonly copy the OAuth resource indicator into
    ``aud``. Some IdPs expose it as a dedicated ``resource`` claim instead, so
    accept either form as long as it matches the configured Menhir resource.
    """
    token_resources = _claim_values(claims.get("aud")) | _claim_values(claims.get("resource"))
    return bool(token_resources.intersection(accepted))


# ---------------------------------------------------------------------------
# Preflight diagnostics
# ---------------------------------------------------------------------------

from .oauth_preflight import build_oauth_preflight
