"""Credential, tier, and request-identity resolution for the auth middleware.

Mixin extracted verbatim from ``auth.py``. The facade class
``BearerAuthMiddleware`` composes ``_CredentialResolutionMixin``, so every method
below remains reachable as ``BearerAuthMiddleware.*`` (including the static
``_request_session_headers``) exactly as before the extraction.
"""

from __future__ import annotations

import hashlib
import hmac

from menhir.api.auth_request import (
    Scope,
    _PROXY_FORWARDING_HEADERS,
    _identity_header,
    _is_mcp_path,
)
from menhir.config.settings import is_loopback_host


class _CredentialResolutionMixin:
    """Token matching, tier resolution, and session-identity derivation."""

    @staticmethod
    def _token_matches(token: str, key: str) -> bool:
        """Constant-time token comparison (tracker Q5 — no timing side-channel)."""
        return bool(key) and hmac.compare_digest(token.encode("utf-8"), key.encode("utf-8"))

    @staticmethod
    def _client_is_loopback(scope: Scope) -> bool:
        """Return True when the ASGI request originates from a loopback address."""
        client = scope.get("client")
        host = client[0] if client else ""
        return is_loopback_host(host)

    @staticmethod
    def _has_proxy_forwarding_header(headers: dict[bytes, bytes]) -> bool:
        """Return True when the request carries a reverse-proxy forwarding header.

        A genuine local caller (loopback ``curl``) never sets these; a same-host
        reverse proxy always appends one that the external client cannot strip.
        Used to close the loopback bootstrap window on a proxied deployment,
        where every external request otherwise presents peer ``127.0.0.1`` and
        would satisfy the loopback check (CT-001).
        """
        return any(h in headers for h in _PROXY_FORWARDING_HEADERS)

    def _resolve_tier(self, token: str) -> str | None:
        """Map a bearer token to its tier, or None if the token is invalid."""
        if self._token_matches(token, self._operator_key):
            return "operator"
        if self._token_matches(token, self._agent_key):
            return "agent"
        if self._token_matches(token, self._readonly_key):
            return "readonly"
        return None

    def _effective_tier(self, tier: str) -> str:
        """Clamp authenticated authority while the candidate fence is active."""
        return "readonly" if self._force_readonly else tier

    @staticmethod
    def _request_session_headers(
        headers: dict[bytes, bytes],
        *,
        path: str,
        api_key: str,
        qs: dict[str, list[str]] | None = None,
        default_user_id: str = "",
        default_client_id: str = "",
        default_client_name: str = "",
        trust_identity_headers: bool = True,
    ) -> tuple[str, str, str, str]:
        """Return (user_id, session_id, client_id, client_name).

        When *trust_identity_headers* is ``False`` (OAuth mode),
        ``x-menhir-user-id`` and ``x-menhir-client-id`` headers are ignored so
        that verified OAuth token claims cannot be overridden by caller
        headers.  ``x-menhir-client-name`` is also ignored; the verified
        ``client_name`` claim is used instead.
        ``x-menhir-session-id`` is also ignored so session derivation is
        rooted in the verified identity.

        Only the ``x-menhir-*`` spellings are accepted. They are gated by
        *trust_identity_headers*, so an identity header cannot be used to bypass
        the OAuth-mode restriction above.
        """
        if trust_identity_headers:
            user_id = _identity_header(headers, b"user-id")
            client_id = _identity_header(headers, b"client-id")
            session_id = _identity_header(headers, b"session-id")
            client_name = _identity_header(headers, b"client-name")
            if not client_name and qs:
                client_name = (qs.get("client_name") or [None])[0] or ""
        else:
            user_id = ""
            client_id = ""
            session_id = ""
            client_name = ""

        if _is_mcp_path(path):
            derived_user = user_id or default_user_id or "remote-mcp"
            derived_name = client_name or default_client_name or "remote-mcp"
        else:
            derived_user = user_id or default_user_id or "remote-api"
            derived_name = client_name or default_client_name or "remote-api"

        # Derive a stable client_id from the static API key when none is provided.
        # OAuth callers provide the IdP client_id instead; we intentionally do not
        # persist raw OAuth access tokens in session identifiers.
        if not client_id:
            client_id = default_client_id
        if not client_id and api_key:
            client_id = hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]
        # Lowest-priority fallback: derive a stable client_id from a real,
        # non-default client_name (loopback no-auth / static self-declared
        # identity). Gated on trust_identity_headers so it never fires on the
        # OAuth path, where identity comes from the verified token and an empty
        # principal.client_id must stay empty (byte-for-byte unchanged).
        if (
            not client_id
            and trust_identity_headers
            and derived_name not in ("remote-mcp", "remote-api")
        ):
            client_id = hashlib.sha256(derived_name.encode("utf-8")).hexdigest()[:16]

        if not session_id:
            seed_parts = [
                derived_user,
                path,
                headers.get(b"user-agent", b"").decode("latin-1"),
                client_id,
            ]
            if api_key:
                seed_parts.append(api_key)
            digest = hashlib.sha256("|".join(seed_parts).encode("utf-8")).hexdigest()[:16]
            session_id = f"{derived_user}-{digest}"

        return derived_user, session_id, client_id, derived_name
