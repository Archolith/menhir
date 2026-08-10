"""Phase 9 unit coverage: build_oauth_config self-wires the RS verifier to the AS.

When ``MENHIR_OAUTH_AS_ENABLED`` is on and a public base URL is known, the embedded AS
is Menhir's own issuer, so ``build_oauth_config`` must default issuer / JWKS / authorization
server to this host and enable token validation — without clobbering explicit overrides,
and without inventing an issuer when no base URL is configured (fail closed).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from menhir.api.oauth import build_oauth_config

pytestmark = pytest.mark.unit

_BASE = "https://memory.example.com"

# Every MENHIR_OAUTH_* env var build_oauth_config consults as a fallback. Cleared so the
# SimpleNamespace attributes are the only inputs and host env cannot leak into assertions.
_OAUTH_ENV = (
    "MENHIR_OAUTH_ENABLED",
    "MENHIR_OAUTH_AS_ENABLED",
    "MENHIR_PUBLIC_BASE_URL",
    "MENHIR_OAUTH_ISSUER",
    "MENHIR_OAUTH_JWKS_URI",
    "MENHIR_AUTHORIZATION_SERVERS",
    "MENHIR_OAUTH_RESOURCE",
    "MENHIR_MCP_RESOURCE",
    "MENHIR_OAUTH_AUDIENCE",
    "MENHIR_OAUTH_AUDIENCES",
)


@pytest.fixture(autouse=True)
def _clear_oauth_env(monkeypatch):
    for name in _OAUTH_ENV:
        monkeypatch.delenv(name, raising=False)
    yield


def _settings(**overrides) -> SimpleNamespace:
    return SimpleNamespace(**overrides)


def test_as_enabled_self_wires_issuer_jwks_and_authz_server():
    config = build_oauth_config(
        _settings(oauth_as_enabled=True, oauth_public_base_url=_BASE)
    )
    assert config.enabled is True
    assert config.issuer == _BASE
    assert config.jwks_uri == f"{_BASE}/.well-known/jwks.json"
    assert config.authorization_servers == (_BASE,)
    assert config.resource == f"{_BASE}/mcp-http"
    assert config.audiences == (f"{_BASE}/mcp-http",)


def test_explicit_issuer_and_jwks_override_self_default():
    config = build_oauth_config(
        _settings(
            oauth_as_enabled=True,
            oauth_public_base_url=_BASE,
            oauth_issuer="https://external-idp.example.com/",
            oauth_jwks_uri="https://external-idp.example.com/keys",
            oauth_authorization_servers="https://external-idp.example.com",
        )
    )
    assert config.enabled is True
    assert config.issuer == "https://external-idp.example.com/"
    assert config.jwks_uri == "https://external-idp.example.com/keys"
    assert config.authorization_servers == ("https://external-idp.example.com",)


def test_as_enabled_without_base_url_defaults_nothing_and_fails_closed():
    config = build_oauth_config(_settings(oauth_as_enabled=True, oauth_public_base_url=""))
    # enabled follows the AS flag, but with no base URL there is nothing to trust: the
    # verifier will reject on the missing-issuer/JWKS check rather than trust a bad guess.
    assert config.enabled is True
    assert config.issuer == ""
    assert config.jwks_uri == ""
    assert config.authorization_servers == ()


def test_as_disabled_and_rs_disabled_leaves_verifier_off():
    config = build_oauth_config(
        _settings(oauth_as_enabled=False, oauth_public_base_url=_BASE)
    )
    assert config.enabled is False
    assert config.issuer == ""
    assert config.jwks_uri == ""


def test_rs_enabled_alone_is_unchanged_by_as_wiring():
    """The pre-existing resource-server path (external IdP) is untouched when AS is off."""
    config = build_oauth_config(
        _settings(
            oauth_enabled=True,
            oauth_as_enabled=False,
            oauth_public_base_url=_BASE,
            oauth_issuer="https://auth.example.com/",
            oauth_jwks_uri="https://auth.example.com/.well-known/jwks.json",
            oauth_authorization_servers="https://auth.example.com",
        )
    )
    assert config.enabled is True
    assert config.issuer == "https://auth.example.com/"
    assert config.jwks_uri == "https://auth.example.com/.well-known/jwks.json"
    assert config.authorization_servers == ("https://auth.example.com",)


def test_as_enabled_does_not_overwrite_partial_explicit_config():
    """Explicit issuer stays; only the still-empty JWKS/authz-server get self-defaulted."""
    config = build_oauth_config(
        _settings(
            oauth_as_enabled=True,
            oauth_public_base_url=_BASE,
            oauth_issuer="https://custom-issuer.example.com/",
        )
    )
    assert config.issuer == "https://custom-issuer.example.com/"
    assert config.jwks_uri == f"{_BASE}/.well-known/jwks.json"
    assert config.authorization_servers == (_BASE,)
