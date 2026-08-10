"""Tests for OAuth operator preflight diagnostics.

Pure offline tests — no HTTP, no JWKS fetch, no IdP, no server startup.
"""

from __future__ import annotations

import json

import pytest

from menhir.api.oauth import OAuthConfig, build_oauth_preflight


# ===================================================================
# Helpers
# ===================================================================


def _complete_config(**overrides) -> OAuthConfig:
    defaults = dict(
        enabled=True,
        public_base_url="https://memory.example.com",
        resource="https://memory.example.com/mcp-http",
        authorization_servers=("https://auth.example.com",),
        issuer="https://auth.example.com/",
        jwks_uri="https://auth.example.com/.well-known/jwks.json",
        audiences=("https://memory.example.com/mcp-http",),
    )
    defaults.update(overrides)
    return OAuthConfig(**defaults)


def _check_no_secrets(d: dict) -> None:
    dumped = json.dumps(d)
    for secret in (
        "super-secret-oauth-token",
        "super-secret-client-secret",
        "super-secret-url-password",
    ):
        assert secret not in dumped, f"secret leaked: {secret}"


# ===================================================================
# Tests
# ===================================================================


class TestOAuthPreflightDisabled:
    """Disabled OAuth should be clean and quiet."""

    @pytest.mark.unit
    def test_disabled_returns_skip(self):
        result = build_oauth_preflight(OAuthConfig(enabled=False))
        assert result["enabled"] is False
        assert result["status"] == "skip"

    @pytest.mark.unit
    def test_disabled_has_no_noisy_warnings(self):
        result = build_oauth_preflight(OAuthConfig(enabled=False))
        assert len(result["checks"]) == 1
        assert result["checks"][0]["status"] == "skip"


class TestOAuthPreflightEnabled:
    """Enabled OAuth passes with complete config."""

    @pytest.mark.unit
    def test_complete_config_returns_pass(self):
        config = _complete_config()
        result = build_oauth_preflight(config)
        assert result["enabled"] is True
        assert result["status"] == "pass"
        assert all(c["status"] == "pass" for c in result["checks"])

    @pytest.mark.unit
    def test_missing_resource_public_base_url_returns_fail(self):
        config = OAuthConfig(enabled=True)
        result = build_oauth_preflight(config)
        assert result["status"] == "fail"
        oauth_resource_configured = next(
            (c for c in result["checks"] if c["name"] == "oauth_resource_configured"), None
        )
        assert oauth_resource_configured is not None
        assert oauth_resource_configured["status"] == "fail"

    @pytest.mark.unit
    def test_missing_authorization_servers_returns_fail(self):
        config = _complete_config(authorization_servers=())
        result = build_oauth_preflight(config)
        assert result["status"] == "fail"
        auth_servers_check = next(
            (c for c in result["checks"] if c["name"] == "oauth_authorization_servers_configured"), None
        )
        assert auth_servers_check is not None
        assert auth_servers_check["status"] == "fail"

    @pytest.mark.unit
    def test_missing_issuer_returns_fail(self):
        config = _complete_config(issuer="")
        result = build_oauth_preflight(config)
        assert result["status"] == "fail"
        assert any(c["name"] == "oauth_issuer_configured" and c["status"] == "fail" for c in result["checks"])

    @pytest.mark.unit
    def test_missing_jwks_uri_returns_fail(self):
        config = _complete_config(jwks_uri="")
        result = build_oauth_preflight(config)
        assert result["status"] == "fail"
        assert any(c["name"] == "oauth_jwks_uri_configured" and c["status"] == "fail" for c in result["checks"])

    @pytest.mark.unit
    def test_missing_audiences_returns_fail(self):
        config = _complete_config(audiences=())
        result = build_oauth_preflight(config)
        assert result["status"] == "fail"
        assert any(c["name"] == "oauth_audience_configured" and c["status"] == "fail" for c in result["checks"])

    @pytest.mark.unit
    def test_http_non_loopback_public_base_url_warns(self):
        config = _complete_config(public_base_url="http://public.example.com")
        result = build_oauth_preflight(config)
        scheme_checks = [c for c in result["checks"] if c["name"] == "oauth_public_base_url_scheme"]
        assert any(c["status"] == "warn" for c in scheme_checks)

    @pytest.mark.unit
    def test_http_loopback_public_base_url_does_not_warn(self):
        config = _complete_config(public_base_url="http://127.0.0.1:8100")
        result = build_oauth_preflight(config)
        scheme_checks = [c for c in result["checks"] if c["name"] in (
            "oauth_public_base_url_scheme", "oauth_resource_scheme",
            "oauth_authorization_servers_scheme",
        )]
        # No scheme warnings expected
        assert all(c["status"] == "pass" for c in scheme_checks)

    @pytest.mark.unit
    def test_url_credentials_redacted(self):
        config = _complete_config(
            public_base_url="https://user:super-secret-url-password@memory.example.com",
        )
        result = build_oauth_preflight(config)
        dumped = json.dumps(result)
        assert "super-secret-url-password" not in dumped
        cred_check = next(
            (c for c in result["checks"] if c["name"] == "oauth_public_base_url_credentials"), None
        )
        assert cred_check is not None
        assert cred_check["status"] == "warn"

    @pytest.mark.unit
    def test_metadata_url_matches_protected_resource_endpoint(self):
        config = _complete_config()
        result = build_oauth_preflight(config)
        assert result["metadata_url"] == "https://memory.example.com/.well-known/oauth-protected-resource"
        meta_check = next(
            (c for c in result["checks"] if c["name"] == "oauth_metadata_url_available"), None
        )
        assert meta_check is not None and meta_check["status"] == "pass"

    @pytest.mark.unit
    def test_bearer_challenge_includes_resource_metadata(self):
        config = _complete_config()
        challenge = config.challenge()
        assert 'resource_metadata="https://memory.example.com/.well-known/oauth-protected-resource"' in challenge
        meta_check = next(
            (c for c in build_oauth_preflight(config)["checks"]
             if c["name"] == "oauth_challenge_metadata_consistent"), None
        )
        assert meta_check is not None and meta_check["status"] == "pass"

    @pytest.mark.unit
    def test_diagnostics_json_serializable(self):
        config = _complete_config()
        result = build_oauth_preflight(config)
        json.dumps(result)

    @pytest.mark.unit
    def test_diagnostics_no_secrets(self):
        config = _complete_config(
            public_base_url="https://user:super-secret-url-password@memory.example.com",
            resource="https://user:super-secret-client-secret@memory.example.com/resource",
        )
        result = build_oauth_preflight(config)
        _check_no_secrets(result)


class TestOAuthPreflightIntegration:
    """Integration with operator diagnostics."""

    @pytest.mark.unit
    def test_operator_diagnostics_includes_oauth_block(self, monkeypatch: pytest.MonkeyPatch):
        from menhir.operator_diagnostics import build_operator_diagnostics
        from menhir.config import MemorySettings

        monkeypatch.setenv("MENHIR_OAUTH_ENABLED", "true")
        monkeypatch.setenv("MENHIR_PUBLIC_BASE_URL", "https://memory.example.com")
        monkeypatch.setenv("MENHIR_AUTHORIZATION_SERVERS", "https://auth.example.com")
        monkeypatch.setenv("MENHIR_OAUTH_ISSUER", "https://auth.example.com/")
        monkeypatch.setenv("MENHIR_OAUTH_JWKS_URI", "https://auth.example.com/.well-known/jwks.json")
        monkeypatch.setenv("MENHIR_OAUTH_AUDIENCE", "https://memory.example.com/mcp-http")

        settings = MemorySettings.from_env()
        d = build_operator_diagnostics(settings)
        assert "oauth_resource_server" in d
        assert d["oauth_resource_server"]["enabled"] is True

    @pytest.mark.unit
    def test_operator_diagnostics_disabled_oauth(self):
        from menhir.operator_diagnostics import build_operator_diagnostics
        from menhir.config import MemorySettings

        settings = MemorySettings(api_host="127.0.0.1")
        d = build_operator_diagnostics(settings)
        assert "oauth_resource_server" in d
        assert d["oauth_resource_server"]["enabled"] is False
        assert d["oauth_resource_server"]["status"] == "skip"

    @pytest.mark.unit
    def test_operator_diagnostics_oauth_no_secrets(self, monkeypatch: pytest.MonkeyPatch):
        from menhir.operator_diagnostics import build_operator_diagnostics
        from menhir.config import MemorySettings

        monkeypatch.setenv("MENHIR_OAUTH_ENABLED", "true")
        monkeypatch.setenv("MENHIR_PUBLIC_BASE_URL", "https://user:super-secret-url-password@memory.example.com")
        monkeypatch.setenv("MENHIR_AUTHORIZATION_SERVERS", "https://auth.example.com")
        monkeypatch.setenv("MENHIR_OAUTH_ISSUER", "https://auth.example.com/")
        monkeypatch.setenv("MENHIR_OAUTH_JWKS_URI", "https://auth.example.com/.well-known/jwks.json")
        monkeypatch.setenv("MENHIR_OAUTH_AUDIENCE", "https://memory.example.com/mcp-http")

        settings = MemorySettings.from_env()
        d = build_operator_diagnostics(settings)
        _check_no_secrets(d)


class TestOAuthPreflightChecks:
    """Individual check name and behavior tests."""

    @pytest.mark.unit
    def test_issuer_trailing_slash_warning(self):
        config = _complete_config(issuer="https://auth.example.com")
        result = build_oauth_preflight(config)
        assert any(
            c["name"] == "oauth_issuer_trailing_slash" and c["status"] == "warn"
            for c in result["checks"]
        )

    @pytest.mark.unit
    def test_jwks_uri_credentials_warning(self):
        config = _complete_config(
            jwks_uri="https://user:super-secret-url-password@auth.example.com/.well-known/jwks.json",
        )
        result = build_oauth_preflight(config)
        assert any(
            c["name"] == "oauth_jwks_uri_credentials" and c["status"] == "warn"
            for c in result["checks"]
        )
        _check_no_secrets(result)

    @pytest.mark.unit
    def test_jwks_uri_http_non_loopback_warns(self):
        config = _complete_config(
            jwks_uri="http://auth.example.com/.well-known/jwks.json",
        )
        result = build_oauth_preflight(config)
        assert any(
            c["name"] == "oauth_jwks_uri_scheme" and c["status"] == "warn"
            for c in result["checks"]
        )

    @pytest.mark.unit
    def test_jwks_uri_http_loopback_no_warn(self):
        config = _complete_config(
            jwks_uri="http://127.0.0.1:9000/.well-known/jwks.json",
        )
        result = build_oauth_preflight(config)
        assert all(
            c["name"] != "oauth_jwks_uri_scheme" or c["status"] != "warn"
            for c in result["checks"]
        )

    @pytest.mark.unit
    def test_auth_server_credentials_warning(self):
        config = _complete_config(
            authorization_servers=("https://user:super-secret-url-password@auth.example.com",),
        )
        result = build_oauth_preflight(config)
        assert any(
            c["name"] == "oauth_authorization_servers_credentials" and c["status"] == "warn"
            for c in result["checks"]
        )
        _check_no_secrets(result)
