"""Per-auth-mode request handlers for the auth middleware.

Mixins extracted verbatim from ``auth.py``. The facade class
``BearerAuthMiddleware`` composes ``_ClientTokenAuthMixin`` and
``_OAuthAuthMixin``, so ``_call_with_client_token`` and ``_call_with_oauth``
remain reachable on the composed class exactly as before the extraction.
Error responses stay on the facade: they must keep logging under the
``menhir.api.auth`` logger name.
"""

from __future__ import annotations

import asyncio
from urllib.parse import urlencode

from menhir.api.auth_request import (
    LOOPBACK_BOOTSTRAP_ID,
    _ADMIN_MINT_PATH,
    _ADMIN_PATH_PREFIX,
    Receive,
    Scope,
    Send,
)
from menhir.api.oauth import OAuthAuthenticationError
from menhir.core.request_context import (
    bind_request_tool_allowlist,
    reset_request_tool_allowlist,
)
from menhir.mcp.service_access import (
    bind_request_auth_mode,
    bind_request_oauth_context,
    bind_request_session,
    bind_request_tier,
    reset_request_auth_mode,
    reset_request_oauth_context,
    reset_request_session,
    reset_request_tier,
)


class _ClientTokenAuthMixin:
    """The ``AuthMode.CLIENT_TOKEN`` request path."""

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
                # Blocking sqlite read on the auth hot path; `resolve` is a
                # lock-free read-only SELECT, so it is safe off the loop.
                record = await asyncio.to_thread(self._client_token_store.resolve, admin_token)
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
            tier_token = bind_request_tier(self._effective_tier("operator"))
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

        record = (
            await asyncio.to_thread(self._client_token_store.resolve, token_str)
            if token_str
            else None
        )
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
        tier_token = bind_request_tier(self._effective_tier(record.tier))
        try:
            await self.app(scope, receive, send)
        finally:
            reset_request_session(session_token)
            reset_request_auth_mode(auth_mode_token)
            reset_request_tier(tier_token)


class _OAuthAuthMixin:
    """The ``AuthMode.OAUTH`` request path."""

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

        policy = None
        if self._client_policy is not None:
            try:
                policy = self._client_policy.require_client(
                    client_id=principal.client_id,
                    scopes=principal.scopes,
                    tier=principal.tier,
                )
            except PermissionError as exc:
                await self._send_auth_error(
                    scope,
                    send,
                    status_code=403,
                    detail=str(exc),
                    code="client_policy_denied",
                )
                return

        user_id, session_id, client_id, client_name = self._request_session_headers(
            headers,
            path=scope.get("path", ""),
            api_key="",
            qs=qs,
            default_user_id=principal.subject,
            default_client_id=principal.client_id,
            default_client_name=(
                policy.label if policy is not None else principal.client_name
            ),
            trust_identity_headers=False,
        )
        auth_mode_token = bind_request_auth_mode("oauth")
        oauth_context_token = bind_request_oauth_context(
            self._oauth_config,
            principal.scopes,
        )
        session_token = bind_request_session(user_id, session_id, client_id=client_id, client_name=client_name)
        tier_token = bind_request_tier(self._effective_tier(principal.tier))
        tool_policy_token = (
            bind_request_tool_allowlist(policy.allowed_tools)
            if policy is not None
            else None
        )
        try:
            await self.app(scope, receive, send)
        finally:
            if tool_policy_token is not None:
                reset_request_tool_allowlist(tool_policy_token)
            reset_request_session(session_token)
            reset_request_oauth_context(oauth_context_token)
            reset_request_auth_mode(auth_mode_token)
            reset_request_tier(tier_token)
