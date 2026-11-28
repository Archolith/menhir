"""#950: one unparseable JWK must not break the others or 500 the request."""

from __future__ import annotations

import pytest

from menhir.api import jose_provider
from menhir.api import oauth as oauth_module
from menhir.api.oauth import OAuthAuthenticationError, OAuthConfig, OAuthTokenVerifier


def _good_jwk() -> dict:
    key = jose_provider.generate_signing_key()
    return jose_provider.serialize_key(key, private=False)


def _config() -> OAuthConfig:
    return OAuthConfig(
        enabled=True,
        public_base_url="https://memory.example.com",
        resource="https://memory.example.com/mcp-http",
        authorization_servers=("https://auth.example.com",),
        issuer="https://auth.example.com/",
        jwks_uri="https://auth.example.com/jwks.json",
        audiences=("https://memory.example.com/mcp-http",),
    )


def _patch_client(monkeypatch, payload) -> None:
    class _Resp:
        def raise_for_status(self) -> None:
            return None

        def json(self):
            return payload

    class _Client:
        def __init__(self, *a, **k) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc) -> None:
            return None

        async def get(self, url):
            return _Resp()

    monkeypatch.setattr(oauth_module.httpx, "AsyncClient", _Client)


def test_parse_jwks_skips_bad_key_and_keeps_good_one() -> None:
    good = _good_jwk()
    keyset = jose_provider.parse_jwks(
        {"keys": [{"kty": "BOGUS", "kid": "bad"}, good]}
    )
    assert jose_provider.jwks_has_kid(keyset, good["kid"])
    assert not jose_provider.jwks_has_kid(keyset, "bad")


def test_parse_jwks_no_usable_key_raises_jose_error() -> None:
    with pytest.raises(jose_provider.JoseError):
        jose_provider.parse_jwks({"keys": [{"kty": "BOGUS", "kid": "bad"}]})
    with pytest.raises(jose_provider.JoseError):
        jose_provider.parse_jwks({"keys": []})


@pytest.mark.asyncio
async def test_load_jwks_with_one_bad_key_still_loads(monkeypatch) -> None:
    good = _good_jwk()
    _patch_client(monkeypatch, {"keys": [{"kty": "BOGUS"}, good]})
    verifier = OAuthTokenVerifier(_config())
    keyset = await verifier._load_jwks()
    assert jose_provider.jwks_has_kid(keyset, good["kid"])


@pytest.mark.asyncio
async def test_load_jwks_with_no_usable_key_is_oauth_error_not_jose(monkeypatch) -> None:
    _patch_client(monkeypatch, {"keys": [{"kty": "BOGUS"}]})
    verifier = OAuthTokenVerifier(_config())
    with pytest.raises(OAuthAuthenticationError) as info:
        await verifier._load_jwks()
    assert info.value.error == "server_error"
