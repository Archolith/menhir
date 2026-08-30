"""Tests for the embedded OAuth authorization-server metadata endpoint (Phase 3, RFC 8414)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from menhir.api.auth import BearerAuthMiddleware
from menhir.api.oauth_as_metadata import router as oauth_as_metadata_router

pytestmark = pytest.mark.unit

_WELL_KNOWN = "/.well-known/oauth-authorization-server"


def _client(settings: object) -> TestClient:
    app = FastAPI()
    app.state.settings = settings
    app.include_router(oauth_as_metadata_router)
    return TestClient(app)


def test_disabled_by_default_returns_404():
    client = _client(SimpleNamespace(oauth_as_enabled=False))
    assert client.get(_WELL_KNOWN).status_code == 404


def test_enabled_returns_exact_rfc8414_document():
    settings = SimpleNamespace(
        oauth_as_enabled=True,
        oauth_public_base_url="https://memory.example.com",
    )
    resp = _client(settings).get(_WELL_KNOWN)
    assert resp.status_code == 200
    assert resp.json() == {
        "issuer": "https://memory.example.com",
        "authorization_endpoint": "https://memory.example.com/oauth/authorize",
        "token_endpoint": "https://memory.example.com/oauth/token",
        "registration_endpoint": "https://memory.example.com/oauth/register",
        "jwks_uri": "https://memory.example.com/.well-known/jwks.json",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": ["menhir:read", "menhir:write", "menhir:admin"],
        # archolith-oauth advertises the resource this AS issues tokens for, so a
        # client can confirm the AS/resource pairing before starting a flow.
        "protected_resources": ["https://memory.example.com/mcp-http"],
        # RFC 9207 / CIMD capability flags (truthful advertisement).
        "authorization_response_iss_parameter_supported": True,
        "client_id_metadata_document_supported": True,
    }


def test_endpoints_derive_from_public_base_url():
    settings = SimpleNamespace(
        oauth_as_enabled=True,
        oauth_public_base_url="https://mcp.example.org",
    )
    body = _client(settings).get(_WELL_KNOWN).json()
    assert body["issuer"] == "https://mcp.example.org"
    assert body["token_endpoint"] == "https://mcp.example.org/oauth/token"
    assert body["jwks_uri"] == "https://mcp.example.org/.well-known/jwks.json"


def test_enabled_without_base_url_returns_500():
    client = _client(SimpleNamespace(oauth_as_enabled=True))
    assert client.get(_WELL_KNOWN).status_code == 500


def test_path_suffix_variant_also_served():
    settings = SimpleNamespace(
        oauth_as_enabled=True,
        oauth_public_base_url="https://memory.example.com",
    )
    resp = _client(settings).get(_WELL_KNOWN + "/mcp-http")
    assert resp.status_code == 200
    assert resp.json()["issuer"] == "https://memory.example.com"


def test_served_even_when_resource_server_oauth_enabled():
    settings = SimpleNamespace(
        oauth_as_enabled=True,
        oauth_enabled=True,
        oauth_public_base_url="https://memory.example.com",
    )
    assert _client(settings).get(_WELL_KNOWN).status_code == 200


def test_well_known_exempt_from_auth_while_api_requires_it():
    """The AS metadata path stays outside BearerAuthMiddleware; /api/* stays protected."""
    settings = SimpleNamespace(
        oauth_as_enabled=True,
        oauth_public_base_url="https://memory.example.com",
    )
    app = FastAPI()
    app.state.settings = settings
    app.include_router(oauth_as_metadata_router)

    @app.get("/api/secret")
    async def _secret():  # pragma: no cover - reached only if auth is bypassed
        return {"ok": True}

    wrapped = BearerAuthMiddleware(app, operator_key="s3cret-operator-key")
    client = TestClient(wrapped)

    assert client.get(_WELL_KNOWN).status_code == 200
    assert client.get("/api/secret").status_code == 401


# ---------------------------------------------------------------------------
# Refresh discovery (Phase 2): offline_access is AS-only
# ---------------------------------------------------------------------------


def test_refresh_disabled_omits_offline_access_and_refresh_grant():
    body = _client(
        SimpleNamespace(
            oauth_as_enabled=True,
            oauth_public_base_url="https://memory.example.com",
        )
    ).get(_WELL_KNOWN).json()
    assert "offline_access" not in body["scopes_supported"]
    assert "refresh_token" not in body["grant_types_supported"]


def test_refresh_enabled_adds_offline_access_and_refresh_grant():
    settings = SimpleNamespace(
        oauth_as_enabled=True,
        oauth_public_base_url="https://memory.example.com",
        oauth_as_refresh_tokens_enabled=True,
        oauth_as_refresh_ttl_s=4321,
    )
    body = _client(settings).get(_WELL_KNOWN).json()
    assert "offline_access" in body["scopes_supported"]
    assert "menhir:admin" in body["scopes_supported"]
    assert "refresh_token" in body["grant_types_supported"]


def test_refresh_config_receives_issue_flag_and_ttl():
    from menhir.api.oauth_as_metadata import build_authorization_server_config

    enabled = SimpleNamespace(
        oauth_as_enabled=True,
        oauth_public_base_url="https://memory.example.com",
        oauth_as_refresh_tokens_enabled=True,
        oauth_as_refresh_ttl_s=4321,
    )
    config = build_authorization_server_config(enabled)
    assert config.issue_refresh_tokens is True
    assert config.refresh_token_ttl_s == 4321

    disabled = SimpleNamespace(
        oauth_as_enabled=True,
        oauth_public_base_url="https://memory.example.com",
    )
    config = build_authorization_server_config(disabled)
    assert config.issue_refresh_tokens is False


def test_agent_smith_client_metadata_is_stable_public_client():
    settings = SimpleNamespace(
        oauth_as_enabled=True,
        oauth_public_base_url="https://memory.example.com",
        oauth_as_refresh_tokens_enabled=True,
        oauth_scopes_supported=("menhir:read", "menhir:write"),
        oauth_admin_scopes=(),
    )

    response = _client(settings).get(
        "/oauth/client-metadata/agent-smith.json?client=claude"
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    payload = response.json()
    assert payload["client_id"] == (
        "https://memory.example.com/oauth/client-metadata/agent-smith.json?client=claude"
    )
    assert payload["client_name"] == "Agent Smith - Claude"
    assert payload["token_endpoint_auth_methods_supported"] == ["none"]
    assert payload["redirect_uris"] == [
        "http://127.0.0.1:43681/oauth/callback",
        "http://localhost:43681/oauth/callback",
    ]
    assert payload["grant_types"] == ["authorization_code", "refresh_token"]
    assert payload["scope"] == "menhir:read menhir:write offline_access"


def test_agent_smith_clients_have_distinct_ids_labels_and_ports():
    from menhir.api.oauth_as_metadata import _AGENT_SMITH_CLIENTS

    settings = SimpleNamespace(
        oauth_as_enabled=True,
        oauth_public_base_url="https://memory.example.com",
        oauth_as_refresh_tokens_enabled=True,
        oauth_scopes_supported=("menhir:read", "menhir:write"),
        oauth_admin_scopes=(),
    )
    client = _client(settings)
    payloads = [
        client.get(
            f"/oauth/client-metadata/agent-smith.json?client={client_key}"
        ).json()
        for client_key in _AGENT_SMITH_CLIENTS
    ]

    assert len(payloads) == 12
    assert len({payload["client_id"] for payload in payloads}) == 12
    assert len({payload["client_name"] for payload in payloads}) == 12
    assert len({payload["redirect_uris"][0] for payload in payloads}) == 12


def test_retired_reasonix_client_is_not_published():
    settings = SimpleNamespace(
        oauth_as_enabled=True,
        oauth_public_base_url="https://memory.example.com",
    )

    response = _client(settings).get(
        "/oauth/client-metadata/agent-smith.json?client=reasonix"
    )

    assert response.status_code == 404


def test_retired_shared_agent_smith_client_is_not_published():
    settings = SimpleNamespace(
        oauth_as_enabled=True,
        oauth_public_base_url="https://memory.example.com",
    )

    response = _client(settings).get("/oauth/client-metadata/agent-smith.json")

    assert response.status_code == 404
