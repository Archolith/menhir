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
"""

from __future__ import annotations

import base64
import hmac
import html
import json
import secrets
import threading
import time
from hashlib import sha256
from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from menhir.api.auth_code_store import get_auth_code_store
from menhir.api.oauth_as_metadata import _as_enabled
from menhir.api.oauth_client_store import OAuthClient, get_client_store
from menhir.api.oauth_rate_limit import FixedWindowLimiter, build_approve_limiter, client_ip
from menhir.config import MemorySettings, build_oauth_config
from menhir.config.oauth import _get_setting

router = APIRouter()

_ADMIN_SUBJECT = "menhir-admin"
_CONSENT_TTL_DEFAULT_S = 300.0

# Phase 8: consent-session cookie (true one-click after the first approval).
_SESSION_COOKIE = "menhir_as_session"
_SESSION_TTL_DEFAULT_S = 600.0
_CONSENT_HEADERS = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
    "Content-Security-Policy": (
        "default-src 'none'; form-action 'self'; "
        "frame-ancestors 'none'; base-uri 'none'"
    ),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}

# Fields signed into the integrity token, in a fixed order.
_SIGNED_FIELDS = (
    "client_id",
    "redirect_uri",
    "scope",
    "code_challenge",
    "code_challenge_method",
    "resource",
    "state",
)

# Per-process integrity-token secret; overridable for tests / determinism.
_PROCESS_CONSENT_SECRET = secrets.token_bytes(32)

# AS-004: throttle failed/approve POSTs per IP so a single consent token cannot be used to
# brute-force the admin secret at speed.
_approve_limiter = FixedWindowLimiter(max_per_window=10, window_s=300)

# AS-004: a consent token is single-use. Each token carries a random ``jti``; once an
# approve POST redeems it, the jti is recorded here (with an expiry) and any replay of the
# same token is rejected. Guarded by a lock and pruned on access so it cannot grow without
# bound. Bounded lifetime == the consent TTL, so entries self-expire.
_spent_consent_jtis: dict[str, float] = {}
_spent_consent_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Settings helpers
# ---------------------------------------------------------------------------


def _settings_for(request: Request) -> object:
    return getattr(request.app.state, "settings", None) or MemorySettings.from_env()


def _operator_key(settings: object) -> str:
    return str(_get_setting(settings, "operator_key", "MENHIR_OPERATOR_KEY", "")).strip()


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

            configured_dir = str(getattr(settings, "oauth_as_dir", "")) if settings else ""
            key_bytes = (oauth_as_db_path(configured_dir) / "oauth_signing_key.json").read_bytes()
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


def _consent_ttl_s(settings: object | None = None) -> float:
    resolved = settings if settings is not None else object()
    return float(
        _get_setting(
            resolved,
            "oauth_as_consent_ttl_s",
            "MENHIR_OAUTH_AS_CONSENT_TTL_S",
            _CONSENT_TTL_DEFAULT_S,
        )
    )


# ---------------------------------------------------------------------------
# Integrity / CSRF token (stateless; no session cookie until Phase 8)
# ---------------------------------------------------------------------------


def _sign_consent(fields: dict[str, str], settings: object | None = None) -> str:
    """Return ``b64(payload).b64(hmac)`` binding *fields* + issue time + a single-use
    ``jti`` nonce (AS-004; recorded server-side on redeem to block replay)."""
    payload = {k: fields.get(k, "") for k in _SIGNED_FIELDS}
    payload["iat"] = int(time.time())
    payload["jti"] = secrets.token_urlsafe(16)
    payload_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    sig = hmac.new(_consent_secret(settings), payload_bytes, sha256).digest()
    return "{}.{}".format(
        base64.urlsafe_b64encode(payload_bytes).rstrip(b"=").decode("ascii"),
        base64.urlsafe_b64encode(sig).rstrip(b"=").decode("ascii"),
    )


def _b64url_decode(segment: str) -> bytes:
    padding = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + padding)


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _verify_consent(
    token: str, submitted: dict[str, str], settings: object | None = None
) -> bool:
    """True iff *token* is well-formed, unexpired, signed by us, and every signed
    field equals the corresponding *submitted* value."""
    if not token or token.count(".") != 1:
        return False
    payload_seg, sig_seg = token.split(".", 1)
    try:
        payload_bytes = _b64url_decode(payload_seg)
        provided_sig = _b64url_decode(sig_seg)
    except Exception:
        return False
    expected_sig = hmac.new(_consent_secret(settings), payload_bytes, sha256).digest()
    if not hmac.compare_digest(provided_sig, expected_sig):
        return False
    try:
        payload = json.loads(payload_bytes)
    except Exception:
        return False
    if not isinstance(payload, dict):
        return False
    iat = payload.get("iat")
    if not isinstance(iat, (int, float)):
        return False
    age = time.time() - float(iat)
    if age < -60 or age > _consent_ttl_s(settings):
        return False
    for field in _SIGNED_FIELDS:
        if str(payload.get(field, "")) != str(submitted.get(field, "")):
            return False
    return True


def _consent_jti(token: str) -> str | None:
    """Return the ``jti`` from a consent *token*'s payload, or None. Only called after
    ``_verify_consent`` has authenticated the token, so the payload is trusted."""
    if not token or token.count(".") != 1:
        return None
    try:
        payload = json.loads(_b64url_decode(token.split(".", 1)[0]))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    jti = payload.get("jti")
    return str(jti) if jti else None


def _consume_consent_jti(jti: str, settings: object | None = None) -> bool:
    """Atomically record *jti* as spent. Return True if it was fresh (redeem allowed),
    False if already spent (replay). Expired entries are pruned on access."""
    now = time.time()
    ttl = _consent_ttl_s(settings)
    with _spent_consent_lock:
        if _spent_consent_jtis:
            for stale in [k for k, exp in _spent_consent_jtis.items() if exp <= now]:
                del _spent_consent_jtis[stale]
        if jti in _spent_consent_jtis:
            return False
        _spent_consent_jtis[jti] = now + ttl
        return True


# ---------------------------------------------------------------------------
# Redirect helpers
# ---------------------------------------------------------------------------


def _redirect(redirect_uri: str, params: dict[str, str]) -> RedirectResponse:
    """302 to *redirect_uri* with *params* merged into its query string."""
    parts = urlsplit(redirect_uri)
    query = parse_qsl(parts.query, keep_blank_values=True)
    query.extend((k, v) for k, v in params.items() if v != "")
    new_query = urlencode(query)
    target = urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, parts.fragment))
    return RedirectResponse(target, status_code=302)


def _error_redirect(redirect_uri: str, error: str, description: str, state: str) -> RedirectResponse:
    params = {"error": error, "error_description": description}
    if state:
        params["state"] = state
    return _redirect(redirect_uri, params)


def _bad_request(message: str) -> HTMLResponse:
    """Direct 400 (untrusted target — never redirect)."""
    body = (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        "<title>Authorization error</title></head><body>"
        "<h1>Authorization request rejected</h1><p>{}</p></body></html>"
    ).format(html.escape(message))
    return HTMLResponse(content=body, status_code=400, headers=_CONSENT_HEADERS)


# ---------------------------------------------------------------------------
# Parameter validation shared by GET and POST
# ---------------------------------------------------------------------------


class _RedirectError(Exception):
    """Raised once a trusted redirect_uri is established but the request is invalid."""

    def __init__(self, error: str, description: str) -> None:
        super().__init__(description)
        self.error = error
        self.description = description


def _resolve_client_and_redirect(
    client_id: str, redirect_uri: str
) -> OAuthClient:
    """Return the client iff it exists and *redirect_uri* exactly matches a registered
    URI. Raises HTTPException-like signalling via ValueError for untrusted targets."""
    if not client_id:
        raise ValueError("Missing client_id")
    client = get_client_store().get(client_id)
    if client is None:
        raise ValueError("Unknown client_id")
    if not redirect_uri or redirect_uri not in client.redirect_uris:
        raise ValueError("redirect_uri does not match a registered redirect URI for this client")
    return client


def _resolve_scope(scope_raw: str, client: OAuthClient) -> str:
    """Return the resolved, space-joined granted scope. Raises _RedirectError on a
    requested scope outside the client's grant."""
    granted = set(client.scopes)
    if not scope_raw.strip():
        return " ".join(client.scopes)
    requested = [s for s in scope_raw.split() if s]
    for s in requested:
        if s not in granted:
            raise _RedirectError("invalid_scope", "Requested scope exceeds the client's granted scopes")
    return " ".join(requested)


def _validate_pkce_and_response(
    response_type: str, code_challenge: str, code_challenge_method: str
) -> None:
    if response_type != "code":
        raise _RedirectError("unsupported_response_type", "Only response_type=code is supported")
    if not code_challenge:
        raise _RedirectError("invalid_request", "code_challenge is required (PKCE)")
    if code_challenge_method != "S256":
        raise _RedirectError("invalid_request", "code_challenge_method must be S256")


# ---------------------------------------------------------------------------
# Consent page
# ---------------------------------------------------------------------------


def _hidden(name: str, value: str) -> str:
    return '<input type="hidden" name="{}" value="{}">'.format(
        html.escape(name, quote=True), html.escape(value, quote=True)
    )


def _render_consent(
    fields: dict[str, str],
    client: OAuthClient,
    *,
    error: str = "",
    settings: object | None = None,
) -> str:
    consent_token = _sign_consent(fields, settings)
    scopes = fields.get("scope", "")
    error_html = (
        '<p style="color:#b00">{}</p>'.format(html.escape(error)) if error else ""
    )
    hidden_inputs = "".join(_hidden(k, fields.get(k, "")) for k in _SIGNED_FIELDS)
    hidden_inputs += _hidden("consent_token", consent_token)
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        "<title>Authorize connection</title></head><body>"
        "<h1>Authorize connection</h1>"
        "{error_html}"
        "<p><strong>{client_name}</strong> (<code>{client_id}</code>) is requesting access.</p>"
        "<p>Scopes: <code>{scopes}</code></p>"
        "<p>Codes will be returned to: <code>{redirect_uri}</code></p>"
        "<form method=\"post\" action=\"/oauth/authorize\">"
        "{hidden_inputs}"
        "<p><label>Admin secret: <input type=\"password\" name=\"admin_secret\" autocomplete=\"off\"></label></p>"
        "<button type=\"submit\" name=\"decision\" value=\"approve\">Approve</button> "
        "<button type=\"submit\" name=\"decision\" value=\"deny\">Deny</button>"
        "</form></body></html>"
    ).format(
        error_html=error_html,
        client_name=html.escape(client.client_name or client.client_id),
        client_id=html.escape(client.client_id),
        scopes=html.escape(scopes),
        redirect_uri=html.escape(fields.get("redirect_uri", "")),
        hidden_inputs=hidden_inputs,
    )


# ---------------------------------------------------------------------------
# Consent session cookie (Phase 8) + shared code issuance
# ---------------------------------------------------------------------------


def _session_ttl_s(settings: object | None = None) -> float:
    resolved = settings if settings is not None else object()
    return float(
        _get_setting(
            resolved,
            "oauth_as_session_ttl_s",
            "MENHIR_OAUTH_AS_SESSION_TTL_S",
            _SESSION_TTL_DEFAULT_S,
        )
    )


def _cookie_secure(settings: object) -> bool:
    base = str(_get_setting(settings, "oauth_public_base_url", "MENHIR_PUBLIC_BASE_URL", "")).strip().lower()
    return base.startswith("https")


def _sign_session(
    sub: str, clients: tuple[str, ...] = (), settings: object | None = None
) -> str:
    """Return a signed consent-session token binding *sub*, the explicitly-approved
    ``client_id`` set, and the issue time. One-click is granted ONLY to clients in this
    set (see the GET handler), so a live session cannot silently authorize an
    attacker-registered client (AS-001)."""
    payload = {
        "kind": "session",
        "sub": sub,
        "clients": sorted(set(clients)),
        "iat": int(time.time()),
    }
    payload_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    sig = hmac.new(_consent_secret(settings), payload_bytes, sha256).digest()
    return "{}.{}".format(_b64url_encode(payload_bytes), _b64url_encode(sig))


def _verify_session(
    token: str, settings: object | None = None
) -> tuple[str, tuple[str, ...]] | None:
    """Return ``(sub, approved_clients)`` iff *token* is well-formed, signed by us, tagged
    as a session, and unexpired; else None. ``approved_clients`` is the set of
    ``client_id``s the admin explicitly approved during this session (AS-001). (Domain-
    separated from the consent token via ``kind`` so neither can be replayed as the other.)"""
    if not token or token.count(".") != 1:
        return None
    payload_seg, sig_seg = token.split(".", 1)
    try:
        payload_bytes = _b64url_decode(payload_seg)
        provided_sig = _b64url_decode(sig_seg)
    except Exception:
        return None
    expected_sig = hmac.new(_consent_secret(settings), payload_bytes, sha256).digest()
    if not hmac.compare_digest(provided_sig, expected_sig):
        return None
    try:
        payload = json.loads(payload_bytes)
    except Exception:
        return None
    if not isinstance(payload, dict) or payload.get("kind") != "session":
        return None
    iat = payload.get("iat")
    if not isinstance(iat, (int, float)):
        return None
    age = time.time() - float(iat)
    if age < -60 or age > _session_ttl_s(settings):
        return None
    sub = payload.get("sub")
    if not sub:
        return None
    clients_raw = payload.get("clients", [])
    if not isinstance(clients_raw, list):
        return None
    return (str(sub), tuple(str(c) for c in clients_raw))


def _set_session_cookie(
    response: RedirectResponse, settings: object, clients: tuple[str, ...]
) -> None:
    response.set_cookie(
        key=_SESSION_COOKIE,
        value=_sign_session(_ADMIN_SUBJECT, clients, settings),
        max_age=int(_session_ttl_s(settings)),
        httponly=True,
        secure=_cookie_secure(settings),
        # Strict (not Lax): the session is only ever used first-party on the authorize
        # page, and Strict blocks the cross-site top-level-GET send that AS-001 abused.
        samesite="strict",
        path="/oauth/authorize",
    )


def _issue_code_redirect(
    *,
    client_id: str,
    redirect_uri: str,
    scope: str,
    code_challenge: str,
    resource: str,
    state: str,
    subject: str,
    settings: object,
) -> RedirectResponse:
    """Issue a single-use code and 302 back to *redirect_uri* (shared by one-click GET
    and POST approve).

    ``resource`` is optional on the authorize request but mandatory on the code: the
    token endpoint requires it, and the exchange rejects a code whose bound resource
    does not match. A client that omits it is bound to this server's canonical
    resource so the code stays redeemable.
    """
    bound_resource = resource or build_oauth_config(settings).resource
    if not bound_resource:
        raise HTTPException(
            status_code=500,
            detail="MENHIR_OAUTH_RESOURCE or MENHIR_PUBLIC_BASE_URL is required",
        )
    code = get_auth_code_store().issue(
        client_id=client_id,
        redirect_uri=redirect_uri,
        scope=scope,
        code_challenge=code_challenge,
        code_challenge_method="S256",
        resource=bound_resource,
        subject=subject,
    )
    params = {"code": code}
    if state:
        params["state"] = state
    return _redirect(redirect_uri, params)


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

    # Untrusted-target validation FIRST — never redirect on these.
    try:
        client = _resolve_client_and_redirect(client_id, redirect_uri)
    except ValueError as exc:
        return _bad_request(str(exc))

    # From here the redirect_uri is proven; protocol errors 302 back to it.
    try:
        _validate_pkce_and_response(
            q.get("response_type", ""),
            q.get("code_challenge", ""),
            q.get("code_challenge_method", ""),
        )
        scope = _resolve_scope(q.get("scope", ""), client)
    except _RedirectError as exc:
        return _error_redirect(redirect_uri, exc.error, exc.description, state)

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
        "resource": q.get("resource", ""),
        "state": state,
    }
    return HTMLResponse(
        content=_render_consent(fields, client, settings=settings),
        status_code=200,
        headers=_CONSENT_HEADERS,
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

    # 1b. Single-use (AS-004): burn the consent token's jti now, before evaluating the
    # secret. A replayed token (the brute-force amplifier) is rejected; each admin-secret
    # attempt therefore requires a fresh consent page (a fresh GET).
    jti = _consent_jti(consent_token)
    if jti is None or not _consume_consent_jti(jti, settings):
        return _bad_request("Consent request has already been used; restart the authorization.")

    # 2. Re-validate untrusted target from scratch (client could have changed).
    try:
        client = _resolve_client_and_redirect(submitted["client_id"], redirect_uri)
    except ValueError as exc:
        return _bad_request(str(exc))

    # 3. Re-validate protocol params (302 back to the proven redirect_uri).
    try:
        _validate_pkce_and_response(
            "code",
            submitted["code_challenge"],
            submitted["code_challenge_method"],
        )
        scope = _resolve_scope(submitted["scope"], client)
    except _RedirectError as exc:
        return _error_redirect(redirect_uri, exc.error, exc.description, state)

    # 4. Denial is safe and needs no secret.
    if decision != "approve":
        return _error_redirect(redirect_uri, "access_denied", "The request was denied", state)

    # 4b. Brute-force throttle (AS-004): rate-limit approve attempts per IP before the
    # secret is ever evaluated, so an attacker cannot rapidly guess the admin secret.
    if not _approve_limiter.allow(client_ip(request, settings)):
        return HTMLResponse(
            content=_render_consent(
                submitted,
                client,
                error="Too many attempts; please wait and try again.",
                settings=settings,
            ),
            status_code=429,
            headers=_CONSENT_HEADERS,
        )

    # 5. Admin gate: an unconfigured operator key can never approve.
    operator_key = _operator_key(settings)
    if not operator_key:
        raise HTTPException(
            status_code=403,
            detail="No admin secret is configured (set MENHIR_OPERATOR_KEY) — cannot approve.",
        )
    if not hmac.compare_digest(admin_secret.encode("utf-8"), operator_key.encode("utf-8")):
        return HTMLResponse(
            content=_render_consent(
                submitted, client, error="Invalid admin secret.", settings=settings
            ),
            status_code=401,
            headers=_CONSENT_HEADERS,
        )

    # 6. Approve: issue a single-use code bound to the admin subject, and remember the
    # approval in a short-lived signed session cookie so repeat authorizes are one-click.
    # Carry forward any clients approved earlier in this session, then add this one, so a
    # returning known client stays one-click while a brand-new client still needs consent.
    prior = _verify_session(request.cookies.get(_SESSION_COOKIE, ""), settings)
    prior_clients = prior[1] if prior else ()
    approved_clients = tuple(sorted(set(prior_clients) | {submitted["client_id"]}))

    response = _issue_code_redirect(
        client_id=submitted["client_id"],
        redirect_uri=redirect_uri,
        scope=scope,
        code_challenge=submitted["code_challenge"],
        resource=submitted["resource"],
        state=state,
        subject=_ADMIN_SUBJECT,
        settings=settings,
    )
    _set_session_cookie(response, settings, approved_clients)
    return response
