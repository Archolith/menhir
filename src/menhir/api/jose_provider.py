"""Library-neutral JOSE provider seam.

The concrete crypto library (joserfc) is confined to this module so that all
JWT/JWK/JWKS operations are isolated behind a narrow interface.  Callers MUST
treat returned key/keyset objects as **opaque handles** — never inspect or
access their attributes directly.  This allows swapping the library (e.g. to
PyJWT) without touching any other module.
"""

from __future__ import annotations

from typing import Any

from joserfc import jwt as _jwt
from joserfc.jwk import KeySet as _KeySet, RSAKey as _RSAKey
from joserfc.jwt import JWTClaimsRegistry as _JWTClaimsRegistry

# ---------------------------------------------------------------------------
# Opaque type aliases
# ---------------------------------------------------------------------------
# Callers MUST NOT introspect these objects; treat them as opaque handles.
KeySetHandle = Any
KeyHandle = Any


# ---------------------------------------------------------------------------
# Provider-neutral error
# ---------------------------------------------------------------------------

class JoseError(Exception):
    """Raised for any JOSE parse/decode/validate failure, normalized across libraries."""


# ---------------------------------------------------------------------------
# JWKS / key-set operations
# ---------------------------------------------------------------------------

def parse_jwks(payload: dict) -> KeySetHandle:
    """Build a key set from a JWKS dict."""
    try:
        return _KeySet.import_key_set(payload)
    except Exception as exc:
        raise JoseError("Unable to parse JWKS") from exc


def jwks_has_kid(keyset: KeySetHandle, kid: str) -> bool:
    """True if *kid* is present in the cached key set; never raises."""
    try:
        keyset.get_by_kid(kid)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# JWT verification
# ---------------------------------------------------------------------------

def verify_jwt(
    token: str,
    keyset: KeySetHandle,
    algorithms: list[str],
    leeway: float,
) -> dict[str, Any]:
    """Decode *token*, enforce algorithm allowlist, validate time claims.

    Returns the claims as a plain dict.  Any library-level failure (expired,
    bad signature, unsupported algorithm, unknown kid, malformed token) is
    normalized to :exc:`JoseError`.
    """
    try:
        decoded = _jwt.decode(token, keyset, algorithms=algorithms)
        _JWTClaimsRegistry(leeway=leeway).validate(decoded.claims)
        return dict(decoded.claims)
    except Exception as exc:
        raise JoseError("Access token signature or claims are invalid") from exc


# ---------------------------------------------------------------------------
# Signing-key generation / serialization
# ---------------------------------------------------------------------------

def generate_signing_key() -> KeyHandle:
    """Generate a 2048-bit RSA private key with an RFC 7638 thumbprint kid."""
    key = _RSAKey.generate_key(2048, private=True)
    key.ensure_kid()
    return key


def serialize_key(key: KeyHandle, *, private: bool) -> dict[str, Any]:
    """Return the JWK dict for *key*.

    When *private* is True the returned dict includes private parameters;
    when False only public material is included.
    """
    return key.as_dict(private=private)


def load_key(data: dict) -> KeyHandle:
    """Reload a key from a stored JWK dict (kid is preserved)."""
    return _RSAKey.import_key(data)


# ---------------------------------------------------------------------------
# JWT signing
# ---------------------------------------------------------------------------

def sign_jwt(header: dict, claims: dict, key: KeyHandle) -> str:
    """Sign a JWT (used by the future /token endpoint).  Returns a string."""
    return _jwt.encode(header, claims, key)
