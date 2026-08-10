"""Contract tests for the library-neutral JOSE provider seam.

These assert the provider's behavior (round-trip, error normalization, alg
pinning) independent of the concrete library, so a future provider swap must
uphold the same contract.
"""

from __future__ import annotations

import time

import pytest

from menhir.api import jose_provider
from menhir.api.jose_provider import JoseError


def _now() -> int:
    return int(time.time())


def test_generate_serialize_parse_sign_verify_round_trip():
    key = jose_provider.generate_signing_key()
    public = jose_provider.serialize_key(key, private=False)
    assert "d" not in public
    kid = public["kid"]

    keyset = jose_provider.parse_jwks({"keys": [public]})
    token = jose_provider.sign_jwt(
        {"alg": "RS256", "kid": kid}, {"sub": "seam-test", "exp": _now() + 60}, key
    )
    claims = jose_provider.verify_jwt(token, keyset, ["RS256"], leeway=5.0)
    assert claims["sub"] == "seam-test"


def test_load_key_preserves_kid():
    key = jose_provider.generate_signing_key()
    private_jwk = jose_provider.serialize_key(key, private=True)
    reloaded = jose_provider.load_key(private_jwk)
    assert (
        jose_provider.serialize_key(reloaded, private=False)["kid"]
        == jose_provider.serialize_key(key, private=False)["kid"]
    )


def test_serialize_private_includes_material_public_does_not():
    key = jose_provider.generate_signing_key()
    assert "d" in jose_provider.serialize_key(key, private=True)
    assert "d" not in jose_provider.serialize_key(key, private=False)


def test_jwks_has_kid_true_and_false():
    key = jose_provider.generate_signing_key()
    public = jose_provider.serialize_key(key, private=False)
    keyset = jose_provider.parse_jwks({"keys": [public]})
    assert jose_provider.jwks_has_kid(keyset, public["kid"]) is True
    assert jose_provider.jwks_has_kid(keyset, "no-such-kid") is False


def test_verify_rejects_malformed_token_as_jose_error():
    key = jose_provider.generate_signing_key()
    keyset = jose_provider.parse_jwks({"keys": [jose_provider.serialize_key(key, private=False)]})
    with pytest.raises(JoseError):
        jose_provider.verify_jwt("not.a.jwt", keyset, ["RS256"], leeway=5.0)


def test_verify_rejects_expired_token_as_jose_error():
    key = jose_provider.generate_signing_key()
    public = jose_provider.serialize_key(key, private=False)
    keyset = jose_provider.parse_jwks({"keys": [public]})
    token = jose_provider.sign_jwt(
        {"alg": "RS256", "kid": public["kid"]}, {"sub": "x", "exp": _now() - 100}, key
    )
    with pytest.raises(JoseError):
        jose_provider.verify_jwt(token, keyset, ["RS256"], leeway=5.0)


def test_verify_enforces_algorithm_allowlist():
    # A token whose alg is outside the allowlist must be rejected (alg pin).
    key = jose_provider.generate_signing_key()
    public = jose_provider.serialize_key(key, private=False)
    keyset = jose_provider.parse_jwks({"keys": [public]})
    token = jose_provider.sign_jwt(
        {"alg": "RS256", "kid": public["kid"]}, {"sub": "x", "exp": _now() + 60}, key
    )
    with pytest.raises(JoseError):
        jose_provider.verify_jwt(token, keyset, ["ES256"], leeway=5.0)


def test_parse_jwks_rejects_garbage_as_jose_error():
    with pytest.raises(JoseError):
        jose_provider.parse_jwks({"keys": "not-a-list"})
