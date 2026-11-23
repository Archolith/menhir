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


_AGENT_SMITH = "/oauth/client-metadata/agent-smith.json?client="
_AGENT_SMITH_SETTINGS = SimpleNamespace(
    oauth_as_enabled=True,
    oauth_public_base_url="https://memory.example.com",
    oauth_as_refresh_tokens_enabled=True,
    oauth_scopes_supported=("menhir:read", "menhir:write", "menhir:admin"),
    oauth_admin_scopes=("menhir:admin",),
)


def agent_smith_policy(tmp_path, issuer="https://memory.example.com", edit=None):
    """Load the v3 synthetic policy with its client ids rebased onto *issuer*."""
    import hashlib
    import json
    from pathlib import Path

    from menhir.api.client_policy import load_client_policy

    source = Path(__file__).resolve().parent / "fixtures" / "client-policy.v3.synthetic.json"
    text = source.read_text(encoding="utf-8").replace("https://menhir.example.com", issuer)
    payload = json.loads(text)
    if edit is not None:
        edit(payload)
    payload.pop("canonical_digest", None)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    digest = hashlib.sha256(canonical.encode("ascii")).hexdigest()
    payload["canonical_digest"] = digest
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return load_client_policy(str(path), digest)


def _policy_client(settings, policy) -> TestClient:
    app = FastAPI()
    app.state.settings = settings
    if policy is not None:
        app.state.client_policy = policy
    app.include_router(oauth_as_metadata_router)
    return TestClient(app)


def test_agent_smith_client_metadata_comes_from_policy_registration(tmp_path):
    client = _policy_client(_AGENT_SMITH_SETTINGS, agent_smith_policy(tmp_path))

    response = client.get(_AGENT_SMITH + "claude")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {
        "client_id": "https://memory.example.com" + _AGENT_SMITH + "claude",
        "client_name": "Agent Smith - Claude",
        "redirect_uris": [
            "http://127.0.0.1:43681/oauth/callback",
            "http://localhost:43681/oauth/callback",
        ],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        "token_endpoint_auth_methods_supported": ["none"],
        "scope": "menhir:read menhir:write menhir:admin offline_access",
    }
    opencode = client.get(_AGENT_SMITH + "opencode").json()
    assert opencode["scope"] == "menhir:read menhir:write offline_access"
    agy = client.get(_AGENT_SMITH + "agy").json()
    assert agy["client_name"] == "Agent Smith - Antigravity CLI"
    assert agy["redirect_uris"] == [
        "http://127.0.0.1:43694/oauth/callback",
        "http://localhost:43694/oauth/callback",
    ]
    assert agy["scope"] == "menhir:read menhir:write offline_access"


def test_agent_smith_clients_have_distinct_ids_names_and_ports(tmp_path):
    policy = agent_smith_policy(tmp_path)
    prefix = "https://memory.example.com" + _AGENT_SMITH
    keys = [cid[len(prefix):] for cid in policy.clients if cid.startswith(prefix)]
    client = _policy_client(_AGENT_SMITH_SETTINGS, policy)
    payloads = [client.get(_AGENT_SMITH + key).json() for key in keys]

    assert len(payloads) == 13
    assert len({payload["client_id"] for payload in payloads}) == 13
    assert len({payload["client_name"] for payload in payloads}) == 13
    assert len({payload["redirect_uris"][0] for payload in payloads}) == 13


def test_agent_smith_metadata_needs_a_client_policy():
    response = _client(_AGENT_SMITH_SETTINGS).get(_AGENT_SMITH + "claude")

    assert response.status_code == 404


def test_client_removed_from_policy_is_not_published(tmp_path):
    cid = "https://memory.example.com" + _AGENT_SMITH + "gemini"

    def drop_gemini(payload):
        del payload["clients"][cid]
        for product in payload["access_contract"]["products"].values():
            if cid in product.get("client_ids", []):
                product["client_ids"].remove(cid)

    policy = agent_smith_policy(tmp_path, edit=drop_gemini)
    client = _policy_client(_AGENT_SMITH_SETTINGS, policy)

    assert client.get(_AGENT_SMITH + "gemini").status_code == 404
    assert client.get(_AGENT_SMITH + "claude").status_code == 200


def test_policy_client_without_registration_is_not_published(tmp_path):
    cid = "https://memory.example.com" + _AGENT_SMITH + "codex"

    def strip_registration(payload):
        del payload["clients"][cid]["registration"]

    policy = agent_smith_policy(tmp_path, edit=strip_registration)

    response = _policy_client(_AGENT_SMITH_SETTINGS, policy).get(_AGENT_SMITH + "codex")

    assert response.status_code == 404


def test_agent_smith_document_refuses_foreign_issuer_ids(tmp_path):
    from menhir.api.oauth_as_metadata import agent_smith_client_document_for_id

    foreign = agent_smith_policy(tmp_path, issuer="https://other.example.com")
    foreign_id = "https://other.example.com" + _AGENT_SMITH + "claude"

    assert foreign.clients.get(foreign_id) is not None
    assert agent_smith_client_document_for_id(foreign_id, _AGENT_SMITH_SETTINGS, foreign) is None


def test_unknown_and_missing_client_keys_are_not_published(tmp_path):
    client = _policy_client(_AGENT_SMITH_SETTINGS, agent_smith_policy(tmp_path))

    assert client.get(_AGENT_SMITH + "reasonix").status_code == 404
    assert client.get("/oauth/client-metadata/agent-smith.json").status_code == 404
    assert client.get(_AGENT_SMITH).status_code == 404
