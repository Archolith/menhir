"""Tests for the single-source-of-truth auth-mode resolver."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from menhir.api.auth_mode import (
    AuthMode,
    auth_mode_from,
    resolve_auth_mode,
    static_keys_present,
)


# ---------------------------------------------------------------------------
# auth_mode_from — the one precedence definition
# ---------------------------------------------------------------------------

def test_precedence_oauth_wins_over_everything():
    assert auth_mode_from(
        oauth_enabled=True, client_tokens_enabled=True, static_keys_present=True
    ) is AuthMode.OAUTH


def test_precedence_client_token_over_static():
    assert auth_mode_from(
        oauth_enabled=False, client_tokens_enabled=True, static_keys_present=True
    ) is AuthMode.CLIENT_TOKEN


def test_precedence_static_when_only_keys():
    assert auth_mode_from(
        oauth_enabled=False, client_tokens_enabled=False, static_keys_present=True
    ) is AuthMode.STATIC


def test_precedence_none_when_nothing():
    assert auth_mode_from(
        oauth_enabled=False, client_tokens_enabled=False, static_keys_present=False
    ) is AuthMode.NONE


# ---------------------------------------------------------------------------
# AuthMode.is_authenticated
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "mode,expected",
    [
        (AuthMode.OAUTH, True),
        (AuthMode.CLIENT_TOKEN, True),
        (AuthMode.STATIC, True),
        (AuthMode.NONE, False),
    ],
)
def test_is_authenticated(mode, expected):
    assert mode.is_authenticated is expected


# ---------------------------------------------------------------------------
# static_keys_present
# ---------------------------------------------------------------------------

def test_static_keys_present_detects_any_key():
    assert static_keys_present(SimpleNamespace(api_key="", operator_key="x", agent_key="", readonly_key="")) is True
    assert static_keys_present(SimpleNamespace(api_key="", operator_key="", agent_key="a", readonly_key="")) is True
    assert static_keys_present(SimpleNamespace(api_key="k", operator_key="", agent_key="", readonly_key="")) is True


def test_static_keys_present_false_when_none():
    assert static_keys_present(
        SimpleNamespace(api_key="", operator_key="", agent_key="", readonly_key="")
    ) is False


# ---------------------------------------------------------------------------
# resolve_auth_mode — settings-driven entry (OAuth read via build_oauth_config)
# ---------------------------------------------------------------------------

def _settings(**over):
    base = dict(
        api_key="", operator_key="", agent_key="", readonly_key="",
        client_tokens_enabled=False,
        # build_oauth_config reads these attrs / env; default disabled
        oauth_public_base_url="", oauth_resource="", oauth_issuer="",
        oauth_jwks_uri="", oauth_audiences=(),
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_resolve_none_by_default(monkeypatch):
    for k in ("MENHIR_OAUTH_ENABLED", "MENHIR_OAUTH_ISSUER", "MENHIR_OAUTH_JWKS_URI"):
        monkeypatch.delenv(k, raising=False)
    assert resolve_auth_mode(_settings()) is AuthMode.NONE


def test_resolve_static(monkeypatch):
    monkeypatch.delenv("MENHIR_OAUTH_ENABLED", raising=False)
    assert resolve_auth_mode(_settings(operator_key="op")) is AuthMode.STATIC


def test_resolve_client_token(monkeypatch):
    monkeypatch.delenv("MENHIR_OAUTH_ENABLED", raising=False)
    # client-token beats static keys
    assert resolve_auth_mode(
        _settings(operator_key="op", client_tokens_enabled=True)
    ) is AuthMode.CLIENT_TOKEN


def test_resolve_oauth(monkeypatch):
    monkeypatch.setenv("MENHIR_OAUTH_ENABLED", "true")
    monkeypatch.setenv("MENHIR_OAUTH_ISSUER", "https://idp.example/")
    monkeypatch.setenv("MENHIR_OAUTH_JWKS_URI", "https://idp.example/jwks")
    monkeypatch.setenv("MENHIR_OAUTH_AUDIENCE", "https://rs.example/mcp-http")
    # OAuth beats client-token and static
    assert resolve_auth_mode(
        _settings(operator_key="op", client_tokens_enabled=True)
    ) is AuthMode.OAUTH
