"""Tests for OAuth protected-resource metadata and auth header trust."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from menhir.api.auth import BearerAuthMiddleware
from menhir.api.oauth_metadata import router as oauth_metadata_router

pytestmark = pytest.mark.unit


def _client(settings: object) -> TestClient:
    app = FastAPI()
    app.state.settings = settings
    app.include_router(oauth_metadata_router)
    return TestClient(app)


def test_oauth_protected_resource_metadata_advertises_resource_and_scopes():
    settings = SimpleNamespace(
        oauth_enabled=True,
        oauth_public_base_url="https://memory.example.com",
        oauth_authorization_servers=("https://auth.example.com",),
        oauth_issuer="https://auth.example.com/",
        oauth_jwks_uri="https://auth.example.com/.well-known/jwks.json",
    )
    client = _client(settings)

    resp = client.get("/.well-known/oauth-protected-resource")

    assert resp.status_code == 200
    assert resp.json() == {
        "resource": "https://memory.example.com/mcp-http",
        "authorization_servers": ["https://auth.example.com"],
        "scopes_supported": ["menhir:read", "menhir:write", "menhir:admin"],
        "bearer_methods_supported": ["header"],
        "resource_name": "Menhir MCP",
    }


def test_oauth_protected_resource_metadata_404_when_disabled():
    client = _client(SimpleNamespace(oauth_enabled=False))

    resp = client.get("/.well-known/oauth-protected-resource")

    assert resp.status_code == 404


# ===================================================================
# T8 — x-yawn-client-name trust in OAuth mode (S-004)
# ===================================================================

class TestClientNameTrust:
    """x-yawn-client-name must be ignored in OAuth mode; honored in static-key mode."""

    def test_oauth_mode_ignores_x_yawn_client_name(self):
        """OAuth mode (trust_identity_headers=False): header client_name is ignored."""
        headers = {b"x-yawn-client-name": b"spoof"}
        _, _, _, derived_name = BearerAuthMiddleware._request_session_headers(
            headers,
            path="/mcp",
            api_key="",
            trust_identity_headers=False,
            default_client_name="token-derived-client",
        )
        assert derived_name == "token-derived-client"

    def test_static_key_mode_honors_x_yawn_client_name(self):
        """Static-key mode (trust_identity_headers=True): header client_name is honored."""
        headers = {b"x-yawn-client-name": b"spoof"}
        _, _, _, derived_name = BearerAuthMiddleware._request_session_headers(
            headers,
            path="/mcp",
            api_key="",
            trust_identity_headers=True,
            default_client_name="default",
        )
        assert derived_name == "spoof"

    def test_oauth_mode_fallback_to_default_when_no_header(self):
        """OAuth mode with no client_name header uses default_client_name."""
        _, _, _, derived_name = BearerAuthMiddleware._request_session_headers(
            {},
            path="/mcp",
            api_key="",
            trust_identity_headers=False,
            default_client_name="token-client",
        )
        assert derived_name == "token-client"

    def test_static_key_mode_empty_header_falls_to_default(self):
        """Static-key mode with empty client_name header uses default."""
        headers = {b"x-yawn-client-name": b""}
        _, _, _, derived_name = BearerAuthMiddleware._request_session_headers(
            headers,
            path="/mcp",
            api_key="",
            trust_identity_headers=True,
            default_client_name="fallback",
        )
        assert derived_name == "fallback"

    def test_oauth_mode_rejects_spoof_via_query_string(self):
        """OAuth mode: client_name query param is ignored when trust=False."""
        headers = {}
        qs = {"client_name": ["spoof-qs"]}
        _, _, _, derived_name = BearerAuthMiddleware._request_session_headers(
            headers,
            path="/mcp",
            api_key="",
            qs=qs,
            trust_identity_headers=False,
            default_client_name="token-client",
        )
        assert derived_name == "token-client"

    def test_static_key_mode_query_string_honored(self):
        """Static-key mode: client_name from query string is used when no header."""
        headers = {}
        qs = {"client_name": ["qs-client"]}
        _, _, _, derived_name = BearerAuthMiddleware._request_session_headers(
            headers,
            path="/mcp",
            api_key="",
            qs=qs,
            trust_identity_headers=True,
            default_client_name="default",
        )
        assert derived_name == "qs-client"
