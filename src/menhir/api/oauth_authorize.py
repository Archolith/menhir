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
import logging
import secrets
import threading
import time
from hashlib import sha256
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from archolith_oauth import (
    consent_request_digest,
    resolve_client_metadata_document as _shared_cimd_resolver,
)

from menhir.api.auth_code_store import get_auth_code_store
from menhir.api.client_policy import ClientPolicy, ClientPolicyAuthority
from menhir.api.oauth_as_metadata import _as_enabled
from menhir.api.oauth_client_store import (
    OAuthClient,
    cimd_fetched_at,
    get_client_store,
    upsert_cimd_client,
)
from menhir.api.oauth_rate_limit import (  # noqa: F401 - test reset seam
    FixedWindowLimiter,
    build_approve_limiter,
    client_ip,
)
from menhir.config import MemorySettings, build_oauth_config
from menhir.config.oauth import _get_setting

router = APIRouter()
logger = logging.getLogger(__name__)

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

# Injectable CIMD document resolver (tests inject fakes; production falls back to
# the shared SSRF-guarded resolver). Signature: async (url) -> dict.
_cimd_resolver: Any | None = None

# Default bounded CIMD snapshot freshness: 24h.
_CIMD_DEFAULT_MAX_AGE_S = 86400
_MAX_CIMD_CLIENT_NAME_LEN = 255

# AS-004: throttle failed/approve POSTs per IP so a single consent token cannot be used to
# brute-force the admin secret at speed.
_approve_limiter = FixedWindowLimiter(max_per_window=10, window_s=300)

# ---------------------------------------------------------------------------
# Settings helpers
# ---------------------------------------------------------------------------


def _settings_for(request: Request) -> object:
    return getattr(request.app.state, "settings", None) or MemorySettings.from_env()


def _production_client_policy(
    request: Request,
    *,
    client_id: str,
    scopes: frozenset[str] | None = None,
) -> ClientPolicy | None:
    """Resolve the production policy before an OAuth authority mutation.

    Non-production/dev applications do not install a policy authority and retain
    their existing OAuth behavior. A production application always installs one.
    """

    authority = getattr(request.app.state, "client_policy", None)
    if authority is None:
        return None
    if not isinstance(authority, ClientPolicyAuthority):
        raise PermissionError("Production client policy authority is invalid")
    if scopes is None:
        return authority.policy_for_client_id(client_id)
    return authority.require_authorization(client_id=client_id, scopes=scopes)


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


def _consent_request_digest(fields: dict[str, str]) -> str:
    return consent_request_digest(
        client_id=fields.get("client_id", ""),
        redirect_uri=fields.get("redirect_uri", ""),
        scope=fields.get("scope", ""),
        code_challenge=fields.get("code_challenge", ""),
        code_challenge_method=fields.get("code_challenge_method", ""),
        resource=fields.get("resource", ""),
        subject=_ADMIN_SUBJECT,
        state=fields.get("state", ""),
    )


class _ConsentCapacityError(RuntimeError):
    """The bounded durable consent table cannot admit another live request."""


def _register_consent_jti(
    token: str,
    fields: dict[str, str],
    settings: object | None,
) -> None:
    payload = json.loads(_b64url_decode(token.split(".", 1)[0]))
    jti = str(payload["jti"])
    expires_at = float(payload["iat"]) + _consent_ttl_s(settings)
    if not get_auth_code_store().register_consent_nonce(
        jti=jti,
        client_id=fields.get("client_id", ""),
        expires_at=expires_at,
        request_digest=_consent_request_digest(fields),
    ):
        raise _ConsentCapacityError("durable consent request capacity is exhausted")


def _consume_registered_consent_jti(jti: str, fields: dict[str, str]) -> bool:
    return get_auth_code_store().consume_consent_nonce(
        jti=jti,
        request_digest=_consent_request_digest(fields),
    )


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


def _as_issuer(settings: object) -> str:
    """Exact AS issuer (RFC 9207): must byte-match the advertised metadata issuer."""
    from menhir.api.oauth_as_metadata import build_authorization_server_config

    return build_authorization_server_config(settings).issuer


def _error_redirect(
    redirect_uri: str,
    error: str,
    description: str,
    state: str,
    settings: object,
) -> RedirectResponse:
    params = {"iss": _as_issuer(settings), "error": error, "error_description": description}
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


def _is_https_url(value: str) -> bool:
    """True only for a well-formed HTTPS URL with a host (CIMD identifier shape)."""
    try:
        parts = urlsplit(value)
    except Exception:
        return False
    return parts.scheme == "https" and bool(parts.hostname)


def _stale_client_max_age_s(settings: object) -> int:
    """Bounded CIMD snapshot freshness (default 24h, shared with DCR reaping)."""
    return int(
        _get_setting(
            settings,
            "oauth_as_stale_client_max_age_s",
            "MENHIR_OAUTH_AS_STALE_CLIENT_MAX_AGE_S",
            _CIMD_DEFAULT_MAX_AGE_S,
        )
    )


def refresh_tokens_enabled(settings: object) -> bool:
    """True iff the AS issues refresh tokens (drives offline_access availability)."""
    from menhir.api.oauth_as_register import refresh_tokens_enabled as _flag

    return _flag(settings)


def _as_scopes_for_clients(settings: object) -> tuple[str, ...]:
    """Full configured AS scope surface for single-owner-profile CIMD clients;
    authorize still validates the requested subset. Includes offline_access only
    when refresh tokens are enabled."""
    from menhir.api.oauth_as_register import as_scope_surface

    return as_scope_surface(settings)


def _client_from_cimd_document(
    client_id: str, doc: Any, settings: object
) -> OAuthClient:
    """Convert resolver-validated metadata into an OAuthClient. Fails closed on
    any identity/shape/auth-method deviation."""
    from menhir.api.oauth_as_register import _redirect_uri_ok

    if not isinstance(doc, dict):
        raise ValueError("Client metadata document must be a JSON object")
    # Exact identity: the document's client_id must byte-match the URL used.
    if str(doc.get("client_id", "")) != client_id:
        raise ValueError("Metadata document client_id does not match the requesting identifier")
    redirect_uris_raw = doc.get("redirect_uris")
    if (
        not isinstance(redirect_uris_raw, list)
        or not redirect_uris_raw
        or not all(isinstance(u, str) and u and _redirect_uri_ok(u) for u in redirect_uris_raw)
    ):
        raise ValueError("Metadata document redirect_uris are invalid")
    auth_method = doc.get("token_endpoint_auth_method", "none")
    supported_auth_methods = doc.get("token_endpoint_auth_methods_supported")
    offers_public_client_auth = auth_method == "none" or (
        isinstance(supported_auth_methods, list)
        and all(isinstance(method, str) for method in supported_auth_methods)
        and "none" in supported_auth_methods
    )
    if not offers_public_client_auth:
        raise ValueError("Only public clients (token endpoint auth method 'none') are supported")
    client_name = str(doc.get("client_name", "")).strip()[:_MAX_CIMD_CLIENT_NAME_LEN]
    return OAuthClient(
        client_id=client_id,
        client_name=client_name,
        redirect_uris=tuple(str(u) for u in redirect_uris_raw),
        scopes=_as_scopes_for_clients(settings),
        client_secret_hash="",
        # Persist the method selected by this AS, not the client's preferred
        # default.  Current ChatGPT CIMD metadata prefers private_key_jwt but
        # explicitly offers both private_key_jwt and none.
        token_endpoint_auth_method="none",
        created_at=time.time(),
    )


async def resolve_cimd_client(client_id: str, settings: object) -> OAuthClient:
    """Resolve an HTTPS URL client_id via CIMD with durable bounded-freshness caching.

    A fresh cached snapshot is used without network; stale or missing snapshots
    trigger revalidation through the shared SSRF-safe resolver and are durably
    upserted under the exact URL client_id. Revalidation failure fails closed.
    Token/code exchange may still use the durable OAuthClient row after restart.
    """
    from menhir.api.oauth_as_metadata import agent_smith_client_document_for_id

    local_document = agent_smith_client_document_for_id(client_id, settings)
    if local_document is not None:
        client = _client_from_cimd_document(client_id, local_document, settings)
        upsert_cimd_client(client, fetched_at=time.time())
        return client

    store = get_client_store()
    now = time.time()
    cached = store.get(client_id)
    fetched_at = cimd_fetched_at(client_id)
    max_age = _stale_client_max_age_s(settings)
    if (
        cached is not None
        and fetched_at is not None
        and max_age > 0
        and (now - fetched_at) <= max_age
    ):
        if cached.token_endpoint_auth_method != "none":
            raise ValueError("Cached client metadata is not a public client")
        return cached

    resolver = _cimd_resolver or _shared_cimd_resolver
    try:
        doc = await resolver(client_id)
    except Exception as exc:
        logger.warning(
            "CIMD document retrieval or validation failed (%s)",
            type(exc).__name__,
        )
        raise ValueError("CIMD document could not be retrieved or validated") from exc
    client = _client_from_cimd_document(client_id, doc, settings)
    upsert_cimd_client(client, fetched_at=now)
    return client


async def _resolve_client_and_redirect(
    client_id: str, redirect_uri: str, settings: object
) -> OAuthClient:
    """Return the client iff it exists and *redirect_uri* exactly matches a registered
    URI. Ordinary persisted (DCR) client IDs resolve from the store; HTTPS URL
    client_ids resolve via the SSRF-safe CIMD path. Raises HTTPException-like
    signalling via ValueError for untrusted targets."""
    if not client_id:
        raise ValueError("Missing client_id")
    if _is_https_url(client_id):
        client = await resolve_cimd_client(client_id, settings)
    else:
        client = get_client_store().get(client_id)
    if client is None:
        raise ValueError("Unknown client_id")
    if not redirect_uri or redirect_uri not in client.redirect_uris:
        raise ValueError("redirect_uri does not match a registered redirect URI for this client")
    return client


def _resolve_scope(scope_raw: str, client: OAuthClient, settings: object) -> str:
    """Return the resolved, space-joined granted scope. Raises _RedirectError on a
    requested scope outside the client's grant."""
    currently_supported = set(_as_scopes_for_clients(settings))
    granted = set(client.scopes) & currently_supported
    if not scope_raw.strip():
        return " ".join(scope for scope in client.scopes if scope in granted)
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
    _register_consent_jti(consent_token, fields, settings)
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
    consent_jti: str | None = None,
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
    code_store = get_auth_code_store()
    if consent_jti is None:
        code = code_store.issue(
            client_id=client_id,
            redirect_uri=redirect_uri,
            scope=scope,
            code_challenge=code_challenge,
            code_challenge_method="S256",
            resource=bound_resource,
            subject=subject,
        )
    else:
        code = code_store.issue_with_consent_nonce(
            jti=consent_jti,
            state=state,
            client_id=client_id,
            redirect_uri=redirect_uri,
            scope=scope,
            code_challenge=code_challenge,
            code_challenge_method="S256",
            resource=bound_resource,
            subject=subject,
        )
        if code is None:
            raise PermissionError(
                "Consent request has already been used, expired, changed, or is missing"
            )
    params = {"code": code, "iss": _as_issuer(settings)}
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
    return HTMLResponse(content=content, status_code=200, headers=_CONSENT_HEADERS)


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
    # Carry forward prior approvals. A digest-bound production consent group expands
    # only to clients with identical authority, allowing one operator-secret entry for
    # a managed application suite while preserving a distinct OAuth identity per app.
    # Dynamic/unlisted clients remain strictly client-scoped (AS-001).
    prior = _verify_session(request.cookies.get(_SESSION_COOKIE, ""), settings)
    prior_clients = prior[1] if prior else ()
    authority = getattr(request.app.state, "client_policy", None)
    if isinstance(authority, ClientPolicyAuthority):
        newly_approved = authority.consent_group_clients(submitted["client_id"])
    else:
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
