"""Pure ASGI auth middleware — avoids BaseHTTPMiddleware SSE stream buffering."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Callable, Sequence
from urllib.parse import parse_qs, urlencode

from menhir.api.client_token_store import ClientTokenStore
from menhir.api.errors import error_payload, request_id_for_scope
from menhir.api.oauth import OAuthAuthenticationError, OAuthTokenVerifier
from menhir.config.auth_mode import AuthMode, auth_mode_from
from menhir.config.oauth import OAuthConfig
from menhir.config.settings import is_loopback_host
from menhir.mcp.service_access import (
    bind_request_auth_mode,
    bind_request_session,
    bind_request_tier,
    reset_request_auth_mode,
    reset_request_session,
    reset_request_tier,
)

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

# Human-readable labels for the auth error envelope's top-level ``error`` field.
_STATUS_ERROR_LABELS = {
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    503: "Service Unavailable",
}
_SENSITIVE_SINGLETON_HEADERS = frozenset(
    {
        b"authorization",
        *(f"x-{prefix}-{suffix}".encode() for prefix in ("menhir", "yawn") for suffix in (
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
    """Read a caller identity header, preferring ``x-menhir-*`` over legacy ``x-yawn-*``.

    The ``x-yawn-*`` spelling predates the rename and is still sent by older client
    configurations. It is accepted as a deprecated alias and consulted only when the
    canonical header is absent or empty, so a client sending both cannot have the
    legacy value silently win. Callers must apply the same trust gate to the result
    that they would to the canonical header — this helper does no authorization.
    """
    value = headers.get(b"x-menhir-" + suffix, b"").decode("latin-1").strip()
    if not value:
        value = headers.get(b"x-yawn-" + suffix, b"").decode("latin-1").strip()
    return value


def _is_mcp_path(path: str) -> bool:
    """Return True when *path* is an MCP endpoint (SSE root, sub-path, or Streamable HTTP)."""
    return path == "/mcp" or path.startswith("/mcp/") or path == "/mcp-http"


class BearerAuthMiddleware:
    """Checks bearer credentials on protected API and MCP requests.

    Two authentication modes coexist:

    **Static bearer keys** (default when OAuth is disabled)
      - ``MENHIR_API_KEY`` / ``MENHIR_AGENT_KEY`` / ``MENHIR_READONLY_KEY`` /
        ``MENHIR_OPERATOR_KEY``
      - MCP paths additionally accept ``?api_key=`` for query-string auth
      - Skipped entirely when no keys are configured (local dev mode), respecting
        the loopback safety guard in settings

    **OAuth resource-server mode** (all protected HTTP surfaces)
      - ``MENHIR_OAUTH_ENABLED=true``
      - Validates JWT access tokens against a configured IdP JWKS for ``/api/*``,
        ``/mcp``, ``/mcp/*``, and ``/mcp-http``
      - Static bearer keys and wildcard/``?api_key=`` are not accepted on
        OAuth-protected routes
      - Scope ``menhir:read`` → readonly, ``menhir:write`` → agent,
        ``menhir:admin`` → operator
      - Raw OAuth tokens are never used as session identifiers or persisted

    **Per-client token tier** (enforced provenance)
      - Enabled by passing a ``client_token_store``; owns protected auth when
        set (like OAuth): static keys and self-declared identity headers are not
        trusted on protected routes.
      - Each bearer token resolves (by sha256 lookup) to a *registered*
        ``client_id``/``client_name``/``tier``. The registered identity is bound
        with ``trust_identity_headers=False`` so a caller cannot relabel itself
        via ``x-menhir-*`` headers — this is the tamper-proof property.
      - Unknown or revoked tokens are rejected 401.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        api_key: str = "",
        operator_key: str = "",
        agent_key: str = "",
        readonly_key: str = "",
        oauth_config: OAuthConfig | None = None,
        oauth_verifier: OAuthTokenVerifier | None = None,
        client_token_store: ClientTokenStore | None = None,
        loopback_bound: bool = True,
        auth_mode: AuthMode | None = None,
    ) -> None:
        self.app = app
        self._operator_key = operator_key or api_key  # backwards compat: api_key -> operator
        self._agent_key = agent_key
        self._readonly_key = readonly_key
        self._oauth_config = oauth_config or OAuthConfig()
        self._oauth_verifier = oauth_verifier or (
            OAuthTokenVerifier(self._oauth_config) if self._oauth_config.enabled else None
        )
        self._client_token_store = client_token_store
        # Loopback-origin admin is only trusted when the server itself is
        # loopback-bound; on a network bind a same-host reverse proxy would make
        # every request appear to originate from loopback.
        self._loopback_admin_ok = loopback_bound
        self.api_key = self._operator_key
        # Single gate: the effective auth mode. The server passes the
        # settings-resolved mode (SSOT); when constructed directly (tests) we
        # derive it from the same signals via the one precedence function, so
        # object-driven and settings-driven callers can never disagree.
        self._auth_mode = auth_mode or auth_mode_from(
            oauth_enabled=self._oauth_config.enabled,
            client_tokens_enabled=self._client_token_store is not None,
            static_keys_present=bool(
                self._operator_key or self._agent_key or self._readonly_key
            ),
        )

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

        The legacy ``x-yawn-*`` spellings are still accepted as deprecated
        aliases, and are read only when the canonical header is absent. Both
        spellings are gated by *trust_identity_headers* identically, so the
        alias cannot be used to bypass the OAuth-mode restriction above.
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

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        path: str = scope.get("path", "")
        is_mcp = _is_mcp_path(path)
        is_api = path.startswith("/api/")
        is_explorer = path == "/explorer" or path.startswith("/explorer/")
        raw_headers = list(scope.get("headers", []))
        headers = dict(raw_headers)

        # Exempt static assets from auth
        if any(path.startswith(prefix) for prefix in _EXEMPT_PATH_PREFIXES):
            await self.app(scope, receive, send)
            return

        if not is_api and not is_mcp and not is_explorer:
            await self.app(scope, receive, send)
            return

        if path in _EXEMPT_PATHS:
            await self.app(scope, receive, send)
            return

        duplicate_headers = _duplicate_sensitive_headers(raw_headers)
        if duplicate_headers:
            await self._send_auth_error(
                scope,
                send,
                status_code=400,
                detail=f"Duplicate security-sensitive header: {', '.join(duplicate_headers)}",
                code="duplicate_header",
            )
            return

        # Explorer is open to a direct loopback browser, whatever the auth mode. This
        # covers both a loopback-only bind and a local connection to a LAN-bound server;
        # browsers cannot attach a bearer token to ordinary navigation. Forwarded
        # requests are excluded so a same-host reverse proxy cannot turn remote clients
        # into apparent loopback callers.
        #
        # Remote clients remain enforced exactly like /api/* (graph reads AND candidate
        # approve/reject writes), so LAN exposure still requires a credential.
        direct_loopback = self._client_is_loopback(scope) and not self._has_proxy_forwarding_header(headers)
        if is_explorer and (self._loopback_admin_ok or direct_loopback):
            await self.app(scope, receive, send)
            return

        auth_value = headers.get(b"authorization", b"").decode("latin-1")
        qs = parse_qs(scope.get("query_string", b"").decode("latin-1"))

        # CORS preflight: browsers send OPTIONS with an Origin header and never
        # attach Authorization to a preflight. Let it through untouched so the
        # inner CORSMiddleware (when MENHIR_CORS_ORIGINS is configured) can answer
        # it. A preflight carries no data and no credentials, so this is not an
        # auth bypass — without it the middleware would 401 every preflight before
        # CORS could respond, blocking browser clients on protected routes (N-002).
        if scope.get("method") == "OPTIONS" and b"origin" in headers:
            await self.app(scope, receive, send)
            return

        # Single gate: dispatch on the one resolved auth mode. Only /api/* and
        # /mcp* reach here (other paths returned above), so each mode fully owns
        # protected auth for this request.
        if self._auth_mode is AuthMode.OAUTH:
            await self._call_with_oauth(scope, receive, send, headers=headers, auth_value=auth_value, qs=qs)
            return

        if self._auth_mode is AuthMode.CLIENT_TOKEN:
            await self._call_with_client_token(
                scope, receive, send, headers=headers, auth_value=auth_value, qs=qs, path=path, is_mcp=is_mcp
            )
            return

        if self._auth_mode is AuthMode.NONE:
            # Loopback no-auth: no tokens, but still capture self-declared per-client
            # identity for provenance/telemetry. Safe because startup guarantees a
            # loopback bind (bind-safety guard). Labels are cooperative, not an
            # enforced security boundary. Access/tier behavior is unchanged.
            user_id, session_id, client_id, client_name = self._request_session_headers(
                headers, path=path, api_key="", qs=qs, trust_identity_headers=True
            )
            session_token = bind_request_session(
                user_id, session_id, client_id=client_id, client_name=client_name
            )
            try:
                await self.app(scope, receive, send)
            finally:
                reset_request_session(session_token)
            return

        # AuthMode.STATIC — static bearer keys (header or ?api_key= on MCP paths).
        # Query-string API key for MCP paths
        used_query_api_key = False
        if not auth_value.startswith("Bearer ") and is_mcp:
            qs_key = (qs.get("api_key") or [None])[0]
            if qs_key:
                auth_value = f"Bearer {qs_key}"
                used_query_api_key = True

        token_str = auth_value[7:] if auth_value.startswith("Bearer ") else ""
        tier = self._resolve_tier(token_str) if token_str else None
        if tier is not None:
            if used_query_api_key and "api_key" in qs:
                sanitized_qs = {key: values for key, values in qs.items() if key != "api_key"}
                scope = dict(scope)
                scope["query_string"] = urlencode(sanitized_qs, doseq=True).encode("latin-1")
            user_id, session_id, client_id, client_name = self._request_session_headers(
                headers, path=path, api_key=token_str, qs=qs
            )
            auth_mode_token = bind_request_auth_mode("query" if used_query_api_key else "header")
            session_token = bind_request_session(user_id, session_id, client_id=client_id, client_name=client_name)
            tier_token = bind_request_tier(tier)
            try:
                await self.app(scope, receive, send)
            finally:
                reset_request_session(session_token)
                reset_request_auth_mode(auth_mode_token)
                reset_request_tier(tier_token)
            return

        await self._send_auth_error(
            scope,
            send,
            status_code=401,
            detail="Missing or invalid API key",
            code="unauthorized",
        )

    async def _call_with_client_token(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        *,
        headers: dict[bytes, bytes],
        auth_value: str,
        qs: dict[str, list[str]],
        path: str,
        is_mcp: bool,
    ) -> None:
        assert self._client_token_store is not None

        # Admin endpoints need a bootstrap-capable gate because the token tier
        # owns auth but on first use no client token exists yet. To preserve
        # provenance, an unauthenticated loopback caller may ONLY bootstrap a
        # token (mint); every other admin action requires a real operator
        # credential (operator key, or an operator-tier minted token) so it
        # always carries an identity.
        if path.startswith(_ADMIN_PATH_PREFIX):
            admin_token = auth_value[7:] if auth_value.startswith("Bearer ") else ""
            # Bootstrap is POST-mint only; any other admin verb/path (e.g. GET
            # list, revoke) always requires a real operator credential.
            is_mint = path == _ADMIN_MINT_PATH and scope.get("method") == "POST"
            # A forwarding header means the request came through a reverse proxy,
            # so the loopback peer address is the proxy's, not the caller's. Deny
            # bootstrap in that case so an internet caller behind a same-host
            # proxy cannot mint an operator token during the empty-store window
            # (CT-001).
            loopback_ok = (
                self._loopback_admin_ok
                and self._client_is_loopback(scope)
                and not self._has_proxy_forwarding_header(headers)
            )

            admin_identity: tuple[str, str] | None = None  # (client_id, client_name)
            if admin_token and self._token_matches(admin_token, self._operator_key):
                admin_identity = ("operator-key", "operator-key")
            elif admin_token:
                record = self._client_token_store.resolve(admin_token)
                if record is not None and record.tier == "operator":
                    admin_identity = (record.client_id, record.client_name)
            if (
                admin_identity is None
                and loopback_ok
                and is_mint
                and not self._client_token_store.has_active()
            ):
                # Bootstrap (trust on first use): loopback-no-token can mint ONLY
                # while no active token exists yet. Once bootstrapped, minting
                # over HTTP requires a credential. Note the real boundary for a
                # local process is filesystem access to client_tokens.db (a
                # process that can write the DB can mint regardless, and the
                # stdio MCP path is operator-tier by design — see CT-002); this
                # gate stops *remote* credential-free minting, not local ones.
                # Revoking the last active token re-opens bootstrap.
                admin_identity = (LOOPBACK_BOOTSTRAP_ID, LOOPBACK_BOOTSTRAP_ID)

            if admin_identity is None:
                await self._send_auth_error(
                    scope,
                    send,
                    status_code=401,
                    detail=(
                        "Admin access requires the operator key or an operator-tier "
                        "token (loopback origin may only mint a bootstrap token while "
                        "no active token exists)"
                    ),
                    code="unauthorized",
                )
                return

            admin_cid, admin_name = admin_identity
            session_token = bind_request_session(
                admin_cid, admin_cid, client_id=admin_cid, client_name=admin_name
            )
            tier_token = bind_request_tier("operator")
            auth_mode_token = bind_request_auth_mode("admin")
            try:
                await self.app(scope, receive, send)
            finally:
                reset_request_session(session_token)
                reset_request_tier(tier_token)
                reset_request_auth_mode(auth_mode_token)
            return

        token_str = auth_value[7:] if auth_value.startswith("Bearer ") else ""
        used_query = False
        if not token_str and is_mcp:
            qs_tok = (qs.get("api_key") or [None])[0]
            if qs_tok:
                token_str = qs_tok
                used_query = True

        record = self._client_token_store.resolve(token_str) if token_str else None
        if record is None:
            await self._send_auth_error(
                scope,
                send,
                status_code=401,
                detail="Missing, invalid, or revoked client token",
                code="unauthorized",
            )
            return

        # Strip ?api_key= from the query string before the request reaches
        # downstream handlers (mirror the static-key path).
        if used_query and "api_key" in qs:
            sanitized_qs = {key: values for key, values in qs.items() if key != "api_key"}
            scope = dict(scope)
            scope["query_string"] = urlencode(sanitized_qs, doseq=True).encode("latin-1")

        # Bind the REGISTERED identity; do not trust self-declared headers so a
        # caller cannot relabel itself (tamper-proof).
        user_id, session_id, client_id, client_name = self._request_session_headers(
            headers,
            path=path,
            api_key="",
            qs=qs,
            default_user_id=record.client_id,
            default_client_id=record.client_id,
            default_client_name=record.client_name,
            trust_identity_headers=False,
        )
        auth_mode_token = bind_request_auth_mode("query" if used_query else "client_token")
        session_token = bind_request_session(user_id, session_id, client_id=client_id, client_name=client_name)
        tier_token = bind_request_tier(record.tier)
        try:
            await self.app(scope, receive, send)
        finally:
            reset_request_session(session_token)
            reset_request_auth_mode(auth_mode_token)
            reset_request_tier(tier_token)

    async def _call_with_oauth(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        *,
        headers: dict[bytes, bytes],
        auth_value: str,
        qs: dict[str, list[str]],
    ) -> None:
        if self._oauth_verifier is None:
            await self._send_oauth_error(
                scope,
                send,
                OAuthAuthenticationError("server_error", "OAuth verifier is not configured"),
            )
            return

        if not auth_value.startswith("Bearer "):
            await self._send_oauth_error(
                scope,
                send,
                OAuthAuthenticationError("invalid_token", "Missing bearer token"),
            )
            return

        token_str = auth_value[7:]
        try:
            principal = await self._oauth_verifier.verify_access_token(token_str)
        except OAuthAuthenticationError as exc:
            await self._send_oauth_error(scope, send, exc)
            return

        user_id, session_id, client_id, client_name = self._request_session_headers(
            headers,
            path=scope.get("path", ""),
            api_key="",
            qs=qs,
            default_user_id=principal.subject,
            default_client_id=principal.client_id,
            default_client_name=principal.client_name,
            trust_identity_headers=False,
        )
        auth_mode_token = bind_request_auth_mode("oauth")
        session_token = bind_request_session(user_id, session_id, client_id=client_id, client_name=client_name)
        tier_token = bind_request_tier(principal.tier)
        try:
            await self.app(scope, receive, send)
        finally:
            reset_request_session(session_token)
            reset_request_auth_mode(auth_mode_token)
            reset_request_tier(tier_token)

    async def _send_oauth_error(self, scope: Scope, send: Send, exc: OAuthAuthenticationError) -> None:
        # RFC 6750 defines only invalid_request / invalid_token / insufficient_scope
        # as Bearer challenge error codes. ``server_error`` denotes an operational
        # failure on our side — a JWKS/IdP outage or a server misconfiguration —
        # not a caller credential problem. Surface it as 503 with no Bearer error
        # challenge so clients treat it as a transient service failure to retry,
        # rather than a 401 "re-authenticate" that sends them into a token-refresh
        # loop during an outage (N-003).
        if exc.error == "server_error":
            await self._send_auth_error(
                scope,
                send,
                status_code=503,
                detail=exc.description,
                code="server_error",
            )
            return
        await self._send_auth_error(
            scope,
            send,
            status_code=exc.status_code,
            detail=exc.description,
            code=exc.error,
            www_authenticate=self._oauth_config.challenge(
                error=exc.error,
                description=exc.description,
                scope=exc.scope,
            ),
        )

    async def _send_auth_error(
        self,
        scope: Scope,
        send: Send,
        *,
        status_code: int,
        detail: str,
        code: str,
        www_authenticate: str | None = None,
    ) -> None:
        request_id = request_id_for_scope(scope)
        body = json.dumps(
            error_payload(
                error=_STATUS_ERROR_LABELS.get(status_code, "Forbidden"),
                detail=detail,
                code=code,
                request_id=request_id,
            )
        ).encode()
        response_headers = [
            [b"content-type", b"application/json"],
            [b"content-length", str(len(body)).encode()],
        ]
        if www_authenticate:
            response_headers.append([b"www-authenticate", www_authenticate.encode("latin-1")])
        await send(
            {
                "type": "http.response.start",
                "status": status_code,
                "headers": response_headers,
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": body,
            }
        )
