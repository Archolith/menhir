"""Authorization endpoint (`/oauth/authorize`) for the embedded OAuth AS (Phase 6).

Authorization-code + PKCE, public clients only, single-admin consent. The endpoint
sits OUTSIDE ``BearerAuthMiddleware`` (path is neither ``/api/`` nor ``/mcp``), so it
is unauthenticated by spec; approval is gated in-handler by the operator secret.

Security invariants (audited in Phase 10):
  * exact ``redirect_uri`` match against the registered set — no prefix/substring.
  * unknown ``client_id`` / bad ``redirect_uri`` never redirect (open-redirect / code
    leak) — they return a direct 400. All other protocol errors 302 back to the proven
    redirect_uri with an OAuth ``error`` code.
  * PKCE required, ``S256`` only.
  * consent requires the operator secret (constant-time); an unconfigured operator key
    cannot approve.
  * a stateless HMAC integrity token binds the approval to the exact params shown.
  * every value rendered into HTML is escaped (the page carries the admin secret).

Gated by ``MENHIR_OAUTH_AS_ENABLED`` (404 when off).

This module is the facade for the ``oauth_authorize_*`` sibling leaves (settings,
http, tokens, clients, pages, session, issuance): it re-exports every name they
define so existing imports and the module-attribute test seams keep working. The
consent-secret globals, ``_SESSION_SCHEMA_VERSION``, ``_cimd_resolver`` and
``_approve_limiter`` deliberately remain defined here — tests patch them on this
module and the leaf functions read them through it at call time.
"""

from __future__ import annotations

import hmac
import html
import logging
import secrets
import threading
import time  # noqa: F401 - tests patch/read ``oauth_authorize.time``
from hashlib import sha256
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from menhir.api.oauth_as_metadata import _as_enabled
from menhir.api.oauth_authorize_clients import (  # noqa: F401 - facade re-exports
    _CIMD_DEFAULT_MAX_AGE_S,
    _MAX_CIMD_CLIENT_NAME_LEN,
    _RedirectError,
    _as_scopes_for_clients,
    _client_from_cimd_document,
    _is_https_url,
    _resolve_client_and_redirect,
    _resolve_scope,
    _stale_client_max_age_s,
    _validate_pkce_and_response,
    refresh_tokens_enabled,
    resolve_cimd_client,
)
from menhir.api.oauth_authorize_http import (  # noqa: F401 - facade re-exports
    _CONSENT_HEADERS,
    _as_issuer,
    _bad_request,
    _consent_headers_for_redirect,
    _error_redirect,
    _redirect,
)
from menhir.api.oauth_authorize_issuance import _issue_code_redirect
from menhir.api.oauth_authorize_pages import _hidden, _render_consent
from menhir.api.oauth_authorize_session import (  # noqa: F401 - facade re-exports
    _SESSION_COOKIE,
    _SESSION_TTL_DEFAULT_S,
    _cookie_secure,
    _session_ttl_s,
    _set_session_cookie,
    _sign_session,
    _verify_session,
)
from menhir.api.oauth_authorize_settings import (
    _operator_key,
    _production_client_policy,
    _settings_for,
)
from menhir.api.oauth_authorize_tokens import (  # noqa: F401 - facade re-exports
    _ADMIN_SUBJECT,
    _CONSENT_TTL_DEFAULT_S,
    _ConsentCapacityError,
    _SIGNED_FIELDS,
    _b64url_decode,
    _b64url_encode,
    _consent_jti,
    _consent_request_digest,
    _consent_ttl_s,
    _consume_registered_consent_jti,
    _register_consent_jti,
    _sign_consent,
    _verify_consent,
)
from menhir.api.oauth_client_store import OAuthClient
from menhir.api.oauth_rate_limit import (  # noqa: F401 - test reset seam
    FixedWindowLimiter,
    build_approve_limiter,
    client_ip,
)
from menhir.config import build_oauth_config
from menhir.config.oauth import _get_setting

router = APIRouter()
logger = logging.getLogger(__name__)

# Phase 8: consent-session schema version. Defined here (not in the session leaf)
# because tests patch it on this module to simulate legacy tokens; the session
# sign/verify helpers read it through this module at call time.
_SESSION_SCHEMA_VERSION = 3

# Per-process integrity-token secret; overridable for tests / determinism.
_PROCESS_CONSENT_SECRET = secrets.token_bytes(32)

# Injectable CIMD document resolver (tests inject fakes; production falls back to
# the shared SSRF-guarded resolver). Signature: async (url) -> dict.
_cimd_resolver: Any | None = None

# AS-004: throttle failed/approve POSTs per IP so a single consent token cannot be used to
# brute-force the admin secret at speed.
_approve_limiter = FixedWindowLimiter(max_per_window=10, window_s=300)

_PERSISTENT_CONSENT_SECRET: bytes | None = None
_persistent_consent_lock = threading.Lock()


def _persistent_consent_secret(settings: object | None = None) -> bytes:
    """Derive a stable consent/session HMAC secret from the persisted signing-key file
    (AS-003), so consent + one-click work deterministically across workers/restarts without
    an explicit ``MENHIR_OAUTH_AS_CONSENT_SECRET``. Domain-separated from the signing key's
    own use, and no weaker than that key (which every worker already loads). Falls back to
    the per-process random secret if the file cannot be read yet (single-worker dev); that
    fallback is NOT cached, so a later call picks up the key once it exists."""
    global _PERSISTENT_CONSENT_SECRET
    if _PERSISTENT_CONSENT_SECRET is not None:
        return _PERSISTENT_CONSENT_SECRET
    with _persistent_consent_lock:
        if _PERSISTENT_CONSENT_SECRET is not None:
            return _PERSISTENT_CONSENT_SECRET
        try:
            from menhir.infrastructure.paths import oauth_as_db_path

            configured_path = (
                str(getattr(settings, "oauth_signing_key_path", "")).strip()
                if settings
                else ""
            )
            configured_dir = str(getattr(settings, "oauth_as_dir", "")) if settings else ""
            key_path = (
                Path(configured_path)
                if configured_path
                else oauth_as_db_path(configured_dir) / "oauth_signing_key.json"
            )
            key_bytes = key_path.read_bytes()
        except Exception:
            return _PROCESS_CONSENT_SECRET
        _PERSISTENT_CONSENT_SECRET = sha256(b"menhir-as-consent-v1\0" + key_bytes).digest()
        return _PERSISTENT_CONSENT_SECRET


def _consent_secret(settings: object | None = None) -> bytes:
    resolved = settings if settings is not None else object()
    raw = str(
        _get_setting(
            resolved,
            "oauth_as_consent_secret",
            "MENHIR_OAUTH_AS_CONSENT_SECRET",
            "",
        )
    )
    if raw:
        return raw.encode("utf-8")
    return _persistent_consent_secret(resolved)


def _render_consent_retry(
    fields: dict[str, str],
    client: OAuthClient,
    *,
    error: str,
) -> str:
    """Render a failed approve attempt WITHOUT issuing a new consent token (CF-10).

    The consent token is single-use and its jti is burned before the admin secret is checked,
    so one guess should cost one authorization GET. Re-rendering the form here handed back a
    freshly signed token instead, which is what turned a single GET into an unbounded guess
    loop. This page carries no token and no secret field: continuing requires a fresh GET.

    The link target is safe to build from `fields`: they were verified against our own
    signature at step 1 before this page can be reached.
    """
    retry_params = {k: fields.get(k, "") for k in _SIGNED_FIELDS if fields.get(k)}
    retry_params["response_type"] = "code"
    retry_url = "/oauth/authorize?" + urlencode(retry_params)
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        "<title>Authorize connection</title></head><body>"
        "<h1>Authorize connection</h1>"
        "<p style=\"color:#b00\">{error}</p>"
        "<p><strong>{client_name}</strong> (<code>{client_id}</code>) was not authorized.</p>"
        "<p><a href=\"{retry_url}\">Restart the authorization</a> to try again.</p>"
        "</body></html>"
    ).format(
        error=html.escape(error),
        client_name=html.escape(client.client_name or client.client_id),
        client_id=html.escape(client.client_id),
        retry_url=html.escape(retry_url, quote=True),
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/oauth/authorize", include_in_schema=False)
async def authorize_get(request: Request):
    settings = _settings_for(request)
    if not _as_enabled(settings):
        raise HTTPException(status_code=404, detail="OAuth authorization endpoint is not enabled")

    q = request.query_params
    client_id = q.get("client_id", "")
    redirect_uri = q.get("redirect_uri", "")
    state = q.get("state", "")

    try:
        _production_client_policy(request, client_id=client_id)
    except PermissionError as exc:
        return _bad_request(str(exc))

    # Untrusted-target validation FIRST — never redirect on these.
    try:
        client = await _resolve_client_and_redirect(client_id, redirect_uri, settings)
    except ValueError as exc:
        return _bad_request(str(exc))

    # From here the redirect_uri is proven; protocol errors 302 back to it.
    try:
        _validate_pkce_and_response(
            q.get("response_type", ""),
            q.get("code_challenge", ""),
            q.get("code_challenge_method", ""),
        )
        scope = _resolve_scope(q.get("scope", ""), client, settings)
        _production_client_policy(
            request,
            client_id=client_id,
            scopes=frozenset(scope.split()),
        )
    except PermissionError as exc:
        return _error_redirect(
            redirect_uri,
            "unauthorized_client",
            str(exc),
            state,
            settings,
        )
    except _RedirectError as exc:
        return _error_redirect(redirect_uri, exc.error, exc.description, state, settings)

    # One-click (Phase 8): a valid consent-session cookie skips the page and issues a code
    # directly. Validation above always runs first, so a stale cookie cannot bypass the
    # open-redirect / PKCE / scope checks. Requires a still-configured operator key.
    session = _verify_session(request.cookies.get(_SESSION_COOKIE, ""), settings)
    if session and _operator_key(settings):
        session_sub, approved_clients = session
        # One-click ONLY for a client this admin explicitly approved before (AS-001).
        # Any other client — including an attacker-registered one — falls through to the
        # consent page, so a CSRF'd GET cannot silently mint a code.
        if client_id in approved_clients:
            return _issue_code_redirect(
                client_id=client_id,
                redirect_uri=redirect_uri,
                scope=scope,
                code_challenge=q.get("code_challenge", ""),
                resource=q.get("resource", ""),
                state=state,
                subject=session_sub,
                settings=settings,
            )

    fields = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scope,
        "code_challenge": q.get("code_challenge", ""),
        "code_challenge_method": q.get("code_challenge_method", ""),
        "resource": q.get("resource", "") or build_oauth_config(settings).resource,
        "state": state,
    }
    try:
        content = _render_consent(fields, client, settings=settings)
    except _ConsentCapacityError:
        return HTMLResponse(
            content="Consent service is temporarily busy; retry the authorization shortly.",
            status_code=429,
            headers={**_CONSENT_HEADERS, "Retry-After": "5"},
        )
    return HTMLResponse(
        content=content,
        status_code=200,
        headers=_consent_headers_for_redirect(redirect_uri),
    )


@router.post("/oauth/authorize", include_in_schema=False)
async def authorize_post(request: Request):
    settings = _settings_for(request)
    if not _as_enabled(settings):
        raise HTTPException(status_code=404, detail="OAuth authorization endpoint is not enabled")

    form = await request.form()
    submitted = {k: str(form.get(k, "")) for k in _SIGNED_FIELDS}
    consent_token = str(form.get("consent_token", ""))
    decision = str(form.get("decision", ""))
    admin_secret = str(form.get("admin_secret", ""))

    redirect_uri = submitted["redirect_uri"]
    state = submitted["state"]

    # 1. Integrity: the approval must be bound to exactly the params we showed.
    if not _verify_consent(consent_token, submitted, settings):
        return _bad_request("Consent request is invalid or has expired; restart the authorization.")

    jti = _consent_jti(consent_token)
    if jti is None:
        return _bad_request("Consent request is missing its durable nonce; restart the authorization.")

    try:
        _production_client_policy(
            request,
            client_id=submitted["client_id"],
            scopes=frozenset(submitted["scope"].split()),
        )
    except PermissionError as exc:
        if not _consume_registered_consent_jti(jti, submitted):
            return _bad_request("Consent request has already been used; restart the authorization.")
        return _bad_request(str(exc))

    # 2. Re-validate untrusted target from scratch (client could have changed).
    # This runs strictly after the signed consent envelope is verified, so no
    # network (CIMD revalidation) happens for unauthenticated garbage.
    try:
        client = await _resolve_client_and_redirect(
            submitted["client_id"], redirect_uri, settings
        )
    except ValueError as exc:
        if not _consume_registered_consent_jti(jti, submitted):
            return _bad_request("Consent request has already been used; restart the authorization.")
        return _bad_request(str(exc))

    # 3. Re-validate protocol params (302 back to the proven redirect_uri).
    try:
        _validate_pkce_and_response(
            "code",
            submitted["code_challenge"],
            submitted["code_challenge_method"],
        )
        scope = _resolve_scope(submitted["scope"], client, settings)
    except _RedirectError as exc:
        if not _consume_registered_consent_jti(jti, submitted):
            return _bad_request("Consent request has already been used; restart the authorization.")
        return _error_redirect(redirect_uri, exc.error, exc.description, state, settings)

    # 4. Denial is safe and needs no secret.
    if decision != "approve":
        if not _consume_registered_consent_jti(jti, submitted):
            return _bad_request("Consent request has already been used; restart the authorization.")
        return _error_redirect(
            redirect_uri, "access_denied", "The request was denied", state, settings
        )

    # 4b. Brute-force throttle (AS-004): rate-limit approve attempts per IP before the
    # secret is ever evaluated, so an attacker cannot rapidly guess the admin secret.
    if not _approve_limiter.allow(client_ip(request, settings)):
        if not _consume_registered_consent_jti(jti, submitted):
            return _bad_request("Consent request has already been used; restart the authorization.")
        return HTMLResponse(
            content=_render_consent_retry(
                submitted,
                client,
                error="Too many attempts; please wait and try again.",
            ),
            status_code=429,
            headers=_CONSENT_HEADERS,
        )

    # 5. Admin gate: an unconfigured operator key can never approve.
    operator_key = _operator_key(settings)
    if not operator_key:
        if not _consume_registered_consent_jti(jti, submitted):
            return _bad_request("Consent request has already been used; restart the authorization.")
        raise HTTPException(
            status_code=403,
            detail="No admin secret is configured (set MENHIR_OPERATOR_KEY) — cannot approve.",
        )
    if not hmac.compare_digest(admin_secret.encode("utf-8"), operator_key.encode("utf-8")):
        if not _consume_registered_consent_jti(jti, submitted):
            return _bad_request("Consent request has already been used; restart the authorization.")
        return HTMLResponse(
            content=_render_consent_retry(
                submitted, client, error="Invalid admin secret."
            ),
            status_code=401,
            headers=_CONSENT_HEADERS,
        )

    # 6. Approve: issue a single-use code bound to the admin subject, and remember the
    # approval in a short-lived signed session cookie so repeat authorizes are one-click.
    # Carry forward prior approvals, but approve only the exact client_id shown on this
    # consent page. Distinct clients may carry distinct digest-bound authority, so no
    # client may inherit another client's operator approval (AS-001).
    prior = _verify_session(request.cookies.get(_SESSION_COOKIE, ""), settings)
    prior_clients = prior[1] if prior else ()
    newly_approved = (submitted["client_id"],)
    approved_clients = tuple(sorted(set(prior_clients) | set(newly_approved)))

    try:
        response = _issue_code_redirect(
            client_id=submitted["client_id"],
            redirect_uri=redirect_uri,
            scope=scope,
            code_challenge=submitted["code_challenge"],
            resource=submitted["resource"],
            state=state,
            subject=_ADMIN_SUBJECT,
            settings=settings,
            consent_jti=jti,
        )
    except PermissionError as exc:
        return _bad_request(str(exc))
    _set_session_cookie(response, settings, approved_clients)
    return response
