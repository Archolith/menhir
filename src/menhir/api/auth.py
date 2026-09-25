"""Pure ASGI auth middleware — avoids BaseHTTPMiddleware SSE stream buffering."""

from __future__ import annotations

import asyncio  # noqa: F401  (monkeypatch surface: tests patch "menhir.api.auth.asyncio.to_thread")
import json
import logging
from urllib.parse import parse_qs, urlencode

from menhir.api.auth_credentials import _CredentialResolutionMixin
from menhir.api.auth_modes import _ClientTokenAuthMixin, _OAuthAuthMixin
from menhir.api.auth_request import (
    ASGIApp,
    LOOPBACK_BOOTSTRAP_ID,
    Receive,
    Scope,
    Send,
    _EXEMPT_PATH_PREFIXES,
    _EXEMPT_PATHS,
    _duplicate_sensitive_headers,
    _is_mcp_path,
)
from menhir.api.client_policy import ClientPolicyAuthority
from menhir.api.client_token_store import ClientTokenStore
from menhir.api.errors import error_payload, request_id_for_scope
from menhir.api.oauth import OAuthAuthenticationError, OAuthTokenVerifier
from menhir.config.auth_mode import AuthMode, auth_mode_from
from menhir.config.oauth import OAuthConfig
from menhir.mcp.service_access import (
    bind_request_auth_mode,
    bind_request_session,
    bind_request_tier,
    require_trusted_client_identity,
    reset_request_auth_mode,
    reset_request_session,
    reset_request_tier,
)

# Human-readable labels for the auth error envelope's top-level ``error`` field.
logger = logging.getLogger(__name__)

_STATUS_ERROR_LABELS = {
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    503: "Service Unavailable",
}


class BearerAuthMiddleware(_CredentialResolutionMixin, _ClientTokenAuthMixin, _OAuthAuthMixin):
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
        force_readonly: bool = False,
        client_policy: ClientPolicyAuthority | None = None,
    ) -> None:
        self.app = app
        self._operator_key = operator_key or api_key  # backwards compat: api_key -> operator
        self._agent_key = agent_key
        self._readonly_key = readonly_key
        self._reject_duplicate_tier_keys()
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
        self._force_readonly = bool(force_readonly)
        self._client_policy = client_policy
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

    def _reject_duplicate_tier_keys(self) -> None:
        """Refuse to start when two configured tier keys are the same value (CF-31).

        `_resolve_tier` returns on the first match and tries operator first, so a shared value
        silently resolves every caller holding it to the HIGHEST tier it appears in: set
        MENHIR_READONLY_KEY to the operator key and every readonly client is an operator.
        Nothing anywhere validated distinctness, and there is no runtime symptom -- the
        privilege is simply granted.

        Checked here rather than in settings because this is where the three values are finally
        resolved, including the `api_key -> operator` backwards-compatibility fallback above.
        A check on the settings fields alone would miss a legacy `api_key` colliding with a
        separately configured agent or readonly key.

        Empty keys are not configured keys, so they are excluded rather than compared -- the
        ordinary single-key deployment leaves two of the three blank.
        """
        configured = {
            "operator": self._operator_key,
            "agent": self._agent_key,
            "readonly": self._readonly_key,
        }
        names_by_value: dict[str, list[str]] = {}
        for tier_name, key in configured.items():
            if key:
                names_by_value.setdefault(key, []).append(tier_name)
        collisions = sorted(
            tuple(names) for names in names_by_value.values() if len(names) > 1
        )
        if collisions:
            joined = "; ".join(" == ".join(names) for names in collisions)
            raise ValueError(
                "Menhir tier keys must be distinct: "
                f"{joined}. A shared value resolves to the highest matching tier, so the "
                "lower tier grants the higher one's authority."
            )

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
        # CF-8: the forwarding exclusion is now unconditional, which is what the comment above
        # always claimed and the code did not do.
        #
        # It read `(self._loopback_admin_ok or direct_loopback)`. `_loopback_admin_ok` is
        # `loopback_bound` -- a static SERVER-configuration boolean, permanently True on a
        # loopback-bound server -- so the `or` short-circuited and `direct_loopback`, the only
        # term excluding proxy-forwarded requests, was never evaluated. A same-host reverse
        # proxy connects from 127.0.0.1, so it satisfies every remaining condition.
        #
        # Note what is NOT changed: on a loopback bind the BIND is still the boundary, and the
        # peer is not separately required to be loopback. That is deliberate and documented --
        # the explorer is a browser UI and a browser cannot attach a bearer token, so gating it
        # on the peer would make it unreachable rather than hardened. Three existing tests
        # encode that intent, and a first attempt at this fix that copied the admin-mint path's
        # stricter three-way `and` broke all three. The mint path is stricter because it issues
        # credentials; this one has a different contract, and the defect was only ever the
        # unevaluated forwarding term.
        explorer_loopback_ok = (
            self._loopback_admin_ok or self._client_is_loopback(scope)
        ) and not self._has_proxy_forwarding_header(headers)
        if is_explorer and explorer_loopback_ok:
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
            # CF-34: a loopback BIND is not proof the CALLER is local.
            #
            # This mode grants unauthenticated access on the reasoning that startup guarantees a
            # loopback bind, so only a local process can connect. A same-host reverse proxy
            # defeats that: it IS a local process, it connects from 127.0.0.1, and it forwards
            # requests from anywhere. Every protected surface is then reachable with no
            # credential at all -- the widest bypass in this cluster, because there is no
            # credential to get wrong.
            #
            # CF-8 fixed the explorer half of exactly this assumption and its commit recorded
            # that the no-auth half was left open. This is that half. A forwarding header is
            # positive evidence the request was relayed, so the peer address proves nothing
            # about the origin and the mode's own precondition does not hold.
            #
            # Refused rather than downgraded to another mode: there is no credential to fall
            # back to, and silently serving a proxied caller is the defect. The message names
            # the fix, because an operator who genuinely wants remote access needs to configure
            # auth, not remove the header.
            if self._has_proxy_forwarding_header(headers):
                await self._send_auth_error(
                    scope,
                    send,
                    status_code=401,
                    detail=(
                        "This server runs with authentication disabled, which is only safe for "
                        "directly-connected local callers. This request carries a proxy "
                        "forwarding header, so it was relayed and its origin cannot be "
                        "established. Configure an API key or per-client tokens to serve "
                        "proxied or remote clients."
                    ),
                    code="proxied_request_without_auth",
                )
                return

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
            # Tool contracts refuse to run without a bound tier, so this mode must bind one
            # or "open access on loopback" lists tools and then rejects every call. The
            # unauthenticated loopback caller is the operator of a single-operator
            # deployment -- the same authority the loopback bootstrap mint grants -- still
            # clamped to readonly while the candidate fence is active.
            tier_token = bind_request_tier(self._effective_tier("operator"))
            try:
                await self.app(scope, receive, send)
            finally:
                reset_request_tier(tier_token)
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
            tier_token = bind_request_tier(self._effective_tier(tier))
            try:
                # CF-32 at the TRANSPORT boundary, not per-tool. Under static-key auth the
                # caller supplies `client_name`, and both the namespace pin and the tool
                # allowlist key on it with an unknown name meaning "unrestricted" -- so the
                # party being restricted chose whether the restriction applied.
                #
                # `BaseTool.execute` already refuses this, and that check STAYS as
                # defense-in-depth. But it only covers MCP tools: MCP resources and every REST
                # route reached this point with the name already bound and never validated. The
                # boundary belongs here, above dispatch, because every surface passes through
                # it and none of them can opt out.
                try:
                    require_trusted_client_identity()
                except PermissionError as exc:
                    await self._send_auth_error(
                        scope, send, status_code=403, detail=str(exc),
                        code="unknown_client",
                    )
                    return
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

    async def _send_oauth_error(self, scope: Scope, send: Send, exc: OAuthAuthenticationError) -> None:
        # RFC 6750 defines only invalid_request / invalid_token / insufficient_scope
        # as Bearer challenge error codes. ``server_error`` denotes an operational
        # failure on our side — a JWKS/IdP outage or a server misconfiguration —
        # not a caller credential problem. Surface it as 503 with no Bearer error
        # challenge so clients treat it as a transient service failure to retry,
        # rather than a 401 "re-authenticate" that sends them into a token-refresh
        # loop during an outage (N-003).
        if exc.error == "server_error":
            # Log with the request id the client receives so a reported 503 can be
            # found in the server log directly (not by timestamp matching).
            cause = exc.__cause__
            logger.warning(
                "OAuth server_error -> 503: request_id=%s path=%s detail=%s cause=%s",
                request_id_for_scope(scope),
                scope.get("path", ""),
                exc.description,
                type(cause).__name__ if cause is not None else None,
            )
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
