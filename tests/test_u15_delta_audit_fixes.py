"""U15 delta-audit fixes: CF-300 JWKS log redaction, CF-301 retired-flag warning, CF-302 fallback."""

from __future__ import annotations

import logging

import httpx
import pytest

from menhir.api import oauth as oauth_module
from menhir.api.oauth import OAuthConfig, OAuthTokenVerifier, _redact_urls_in_text
from menhir.config import feature_flags
from menhir.config.feature_flags import warn_retired_settings_set_false
from menhir.config.settings_model import MemorySettings


def _config(jwks_uri: str) -> OAuthConfig:
    return OAuthConfig(
        enabled=True,
        public_base_url="https://memory.example.com",
        resource="https://memory.example.com/mcp-http",
        authorization_servers=("https://auth.example.com",),
        issuer="https://auth.example.com/",
        jwks_uri=jwks_uri,
        audiences=("https://memory.example.com/mcp-http",),
    )


@pytest.mark.parametrize(
    "text",
    [
        # httpx-normalised: host lower/upper-cased differently from the configured string
        "timed out connecting to https://user:hunter2@AUTH.Example.COM/jwks.json?sig=topsecret",
        # percent-encoded userinfo and query
        "failed https://us%40er:hun%74er2@auth.example.com/jwks.json?sig=top%73ecret#frag",
        "boom 'https://auth.example.com/jwks.json?token=topsecret'.",
    ],
)
def test_redact_urls_in_text_strips_userinfo_and_query(text):
    out = _redact_urls_in_text(text, "Fallback")
    for secret in ("hunter2", "hun%74er2", "us%40er", "topsecret", "top%73ecret", "frag"):
        assert secret not in out
    assert "auth.example.com" in out.lower()


def test_redact_fails_closed_on_unparseable_url():
    assert _redact_urls_in_text("bad http://[::1/x?secret=1", "ConnectError") == "ConnectError"
    assert _redact_urls_in_text("https://u/ser:pw@host/x", "ConnectError") == "ConnectError"


@pytest.mark.parametrize(
    "text",
    [
        # httpx leaves "'" unencoded inside the URL it quotes (CF-300 review F1).
        "Client error '404' for url 'https://user'SECRET@auth.example.com/jwks?sig=x'",
        "Client error '404' for url 'https://auth.example.com/jwks?sig=a'SECRET''",
        # Relative / protocol-relative redirect targets from the IdP (F4).
        "Redirect response '302 Found' for url 'https://auth.example.com/jwks'\n"
        "Redirect location: '/x?sig=SECRET'",
        "Redirect location: '//u:SECRET@cdn.example.com/jwks'",
    ],
)
def test_redact_urls_in_text_fails_closed_on_quoted_and_relative_urls(text):
    assert "SECRET" not in _redact_urls_in_text(text, "HTTPStatusError")


@pytest.mark.parametrize(
    "uri",
    [
        "https://user:pa/SECRET@auth.example.com/jwks",  # '/' in the password (F2)
        "user:SECRET@auth.example.com/jwks",  # schemeless (F3)
    ],
)
def test_logged_uri_field_fails_closed(uri):
    assert "SECRET" not in oauth_module._loggable_uri(uri)
    assert oauth_module._loggable_uri("https://u:p@auth.example.com/jwks?s=1") == (
        "https://auth.example.com/jwks"
    )


@pytest.mark.asyncio
async def test_real_httpx_error_text_is_redacted_in_log(monkeypatch, caplog):
    uri = "https://user:hunter2@127.0.0.1:1/.well-known/jwks.json?sig=topsecret"
    verifier = OAuthTokenVerifier(_config(uri))
    # Real httpx error text for a refused connection, with a mixed-case host not equal to the config.
    real = httpx.ConnectError("connect failed: https://user:hunter2@LOCALHOST:1/.well-known/jwks.json?sig=topsecret")

    class _Client:
        def __init__(self, *a, **k) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc) -> None:
            return None

        async def get(self, url):
            raise real

    monkeypatch.setattr(oauth_module.httpx, "AsyncClient", _Client)
    with caplog.at_level(logging.WARNING, logger="menhir.api.oauth"):
        with pytest.raises(oauth_module.OAuthAuthenticationError):
            await verifier._load_jwks()
    assert "hunter2" not in caplog.text and "topsecret" not in caplog.text
    assert "kind=ConnectError" in caplog.text


@pytest.mark.asyncio
async def test_real_httpx_request_error_is_redacted_in_log(caplog):
    # Unmocked: a real refused connection through httpx.
    uri = "https://user:hunter2@127.0.0.1:9/jwks.json?sig=topsecret"
    verifier = OAuthTokenVerifier(_config(uri))
    with caplog.at_level(logging.WARNING, logger="menhir.api.oauth"):
        with pytest.raises(oauth_module.OAuthAuthenticationError):
            await verifier._load_jwks()
    assert "hunter2" not in caplog.text and "topsecret" not in caplog.text


_RETIRED_ENV = "MENHIR_FRONTIER_BM25"


def test_retired_false_warns_by_name_only(caplog):
    with caplog.at_level(logging.WARNING, logger=feature_flags.logger.name):
        names = warn_retired_settings_set_false({_RETIRED_ENV: "false", "MENHIR_OTHER": "false"})
    assert names == [_RETIRED_ENV]
    assert _RETIRED_ENV in caplog.text
    assert "MENHIR_OTHER" not in caplog.text
    assert len(caplog.records) == 1


def test_retired_unset_or_true_is_silent(caplog):
    with caplog.at_level(logging.WARNING, logger=feature_flags.logger.name):
        assert warn_retired_settings_set_false({}) == []
        assert warn_retired_settings_set_false({_RETIRED_ENV: "true"}) == []
    assert caplog.records == []


def test_retired_warning_never_logs_value(caplog):
    with caplog.at_level(logging.WARNING, logger=feature_flags.logger.name):
        warn_retired_settings_set_false({_RETIRED_ENV: "FALSE"})
    assert "FALSE" not in caplog.text.replace(_RETIRED_ENV, "")


def test_inferred_self_mode_default_matches_fallback():
    assert MemorySettings.inferred_self_mode == "enforce"
