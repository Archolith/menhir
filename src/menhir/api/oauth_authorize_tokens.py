"""Consent integrity tokens and the durable consent-nonce registry.

Extracted from :mod:`menhir.api.oauth_authorize` (behavior-preserving split):
the stateless signed consent token (fields + ``iat`` + single-use ``jti``), the
base64url helpers, the TTL setting, the digest binding a consent to its exact
parameters, and the bounded durable nonce register/consume calls (AS-004).

The HMAC consent secret itself (``_consent_secret`` and its seam globals) stays
on the facade module because tests patch it there; ``_sign_consent`` and
``_verify_consent`` resolve it through a deferred facade import at call time,
which preserves that patch behavior exactly while keeping this leaf free of
module-level import cycles.
"""

from __future__ import annotations

import base64
import hmac
import json
import secrets
import time
from hashlib import sha256

from archolith_oauth import consent_request_digest

from menhir.api.auth_code_store import get_auth_code_store
from menhir.config.oauth import _get_setting

_ADMIN_SUBJECT = "menhir-admin"
_CONSENT_TTL_DEFAULT_S = 300.0

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


def _sign_consent(fields: dict[str, str], settings: object | None = None) -> str:
    """Return ``b64(payload).b64(hmac)`` binding *fields* + issue time + a single-use
    ``jti`` nonce (AS-004; recorded server-side on redeem to block replay)."""
    from menhir.api import oauth_authorize as _facade  # consent-secret seam (see docstring)

    payload = {k: fields.get(k, "") for k in _SIGNED_FIELDS}
    payload["iat"] = int(time.time())
    payload["jti"] = secrets.token_urlsafe(16)
    payload_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    sig = hmac.new(_facade._consent_secret(settings), payload_bytes, sha256).digest()
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
    from menhir.api import oauth_authorize as _facade  # consent-secret seam (see docstring)

    if not token or token.count(".") != 1:
        return False
    payload_seg, sig_seg = token.split(".", 1)
    try:
        payload_bytes = _b64url_decode(payload_seg)
        provided_sig = _b64url_decode(sig_seg)
    except Exception:
        return False
    expected_sig = hmac.new(_facade._consent_secret(settings), payload_bytes, sha256).digest()
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
