"""Consent-session cookie (Phase 8) signing/verification for the OAuth AS.

Extracted from :mod:`menhir.api.oauth_authorize` (behavior-preserving split):
the signed one-click session token binding the admin subject to the explicitly
approved client set, its TTL/secure-cookie settings, and the cookie setter.

Two seams stay on the facade module because tests patch them there: the
``_SESSION_SCHEMA_VERSION`` global and the consent secret (``_consent_secret``).
``_sign_session`` and ``_verify_session`` resolve both through a deferred facade
import at call time, which preserves that patch behavior exactly while keeping
this leaf free of module-level import cycles.
"""

from __future__ import annotations

import hmac
import json
import time
from hashlib import sha256

from fastapi.responses import RedirectResponse

from menhir.api.oauth_authorize_tokens import (
    _ADMIN_SUBJECT,
    _b64url_decode,
    _b64url_encode,
)
from menhir.config.oauth import _get_setting

# Phase 8: consent-session cookie (true one-click after the first approval).
_SESSION_COOKIE = "menhir_as_session"
_SESSION_TTL_DEFAULT_S = 600.0


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
    from menhir.api import oauth_authorize as _facade  # schema-version/secret seam (see docstring)

    payload = {
        "kind": "session",
        "version": _facade._SESSION_SCHEMA_VERSION,
        "sub": sub,
        "clients": sorted(set(clients)),
        "iat": int(time.time()),
    }
    payload_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    sig = hmac.new(_facade._consent_secret(settings), payload_bytes, sha256).digest()
    return "{}.{}".format(_b64url_encode(payload_bytes), _b64url_encode(sig))


def _verify_session(
    token: str, settings: object | None = None
) -> tuple[str, tuple[str, ...]] | None:
    """Return ``(sub, approved_clients)`` iff *token* is well-formed, signed by us, tagged
    as a session, and unexpired; else None. ``approved_clients`` is the set of
    ``client_id``s the admin explicitly approved during this session (AS-001). (Domain-
    separated from the consent token via ``kind`` so neither can be replayed as the other.)"""
    from menhir.api import oauth_authorize as _facade  # schema-version/secret seam (see docstring)

    if not token or token.count(".") != 1:
        return None
    payload_seg, sig_seg = token.split(".", 1)
    try:
        payload_bytes = _b64url_decode(payload_seg)
        provided_sig = _b64url_decode(sig_seg)
    except Exception:
        return None
    expected_sig = hmac.new(_facade._consent_secret(settings), payload_bytes, sha256).digest()
    if not hmac.compare_digest(provided_sig, expected_sig):
        return None
    try:
        payload = json.loads(payload_bytes)
    except Exception:
        return None
    if (
        not isinstance(payload, dict)
        or payload.get("kind") != "session"
        or payload.get("version") != _facade._SESSION_SCHEMA_VERSION
    ):
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
