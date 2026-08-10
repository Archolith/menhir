"""Phase 9 end-to-end: embedded AS mints a token the resource-server verifier accepts.

Drives the full one-codebase OAuth flow through a single FastAPI app, no live IdP:

    DCR /oauth/register  ->  /oauth/authorize (consent + admin approve)
      ->  /oauth/token (code + PKCE -> RS256 JWT)  ->  GET /mcp/test with that JWT

The protected /mcp route is guarded by the same ``BearerAuthMiddleware`` +
``OAuthTokenVerifier`` used in production. Phase 9 defaults the verifier's issuer/JWKS
to this host (``build_oauth_config`` self-wiring), so the minted token validates with
zero extra config. The only stub is the verifier's outbound JWKS *fetch*, replaced with
the local signing key's public JWKS exactly as ``test_oauth_jwt_verifier`` does — the
crypto path (sign in /oauth/token, verify in the middleware) is real end to end.
"""

from __future__ import annotations

import base64
import hashlib
import re
import time
import unittest.mock as _mock
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from menhir.api import auth_code_store, jose_provider, oauth_client_store, oauth_keys
from menhir.api.auth import BearerAuthMiddleware
from menhir.api.oauth import OAuthTokenVerifier, build_oauth_config
from menhir.api.oauth_as_register import router as oauth_as_register_router
from menhir.api.oauth_authorize import router as oauth_authorize_router
from menhir.api.oauth_keys import get_signing_key, public_jwks
from menhir.api.oauth_metadata import router as oauth_metadata_router
from menhir.api.oauth_token import router as oauth_token_router
from menhir.mcp.service_access import get_request_auth_mode, get_request_session, get_request_tier

pytestmark = pytest.mark.unit

_BASE = "https://memory.example.com"
_CB = "https://app.example.com/cb"
# The authorize endpoint binds codes to this canonical resource when the client omits
# one, and the token exchange requires the same value back.
_RESOURCE = f"{_BASE}/mcp-http"
_OPERATOR_SECRET = "operator-secret-value"
_SETTINGS = SimpleNamespace(
    oauth_as_enabled=True,
    oauth_public_base_url=_BASE,
    operator_key=_OPERATOR_SECRET,
)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("MENHIR_OAUTH_AS_DIR", str(tmp_path))
    monkeypatch.setattr(oauth_client_store, "_client_store_singleton", None, raising=False)
    monkeypatch.setattr(auth_code_store, "_auth_code_store_singleton", None, raising=False)
    monkeypatch.setattr(oauth_keys, "_SIGNING_KEY", None, raising=False)
    yield
    monkeypatch.setattr(oauth_client_store, "_client_store_singleton", None, raising=False)
    monkeypatch.setattr(auth_code_store, "_auth_code_store_singleton", None, raising=False)
    monkeypatch.setattr(oauth_keys, "_SIGNING_KEY", None, raising=False)


def _pkce() -> tuple[str, str]:
    verifier = "e2e-code-verifier-" + "a" * 48
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    return verifier, challenge


def _hidden_fields(html: str) -> dict[str, str]:
    """Extract the consent form's hidden inputs (signed fields + consent_token)."""
    return {
        m.group(1): m.group(2)
        for m in re.finditer(
            r'<input type="hidden" name="([^"]+)" value="([^"]*)">', html
        )
    }


def _build_client() -> TestClient:
    """One app: AS endpoints (outside auth by path) + a protected /mcp route behind the
    real middleware whose verifier is Phase-9 self-wired and points at the local key."""
    app = FastAPI()
    app.state.settings = _SETTINGS
    app.include_router(oauth_metadata_router)  # /.well-known/jwks.json (+ protected-resource)
    app.include_router(oauth_as_register_router)  # /oauth/register
    app.include_router(oauth_authorize_router)  # /oauth/authorize
    app.include_router(oauth_token_router)  # /oauth/token

    @app.get("/mcp/test")
    async def mcp_test():
        session = get_request_session()
        return JSONResponse(
            {
                "auth_mode": get_request_auth_mode(),
                "tier": get_request_tier(),
                "client_id": session.client_id if session else "",
                "client_name": session.client_name if session else "",
                "user_id": session.user_id if session else "",
            }
        )

    config = build_oauth_config(_SETTINGS)
    # Phase 9 self-wiring: the verifier trusts this host as its own issuer.
    assert config.enabled is True
    assert config.issuer == _BASE
    assert config.jwks_uri == f"{_BASE}/.well-known/jwks.json"
    assert config.audiences == (f"{_BASE}/mcp-http",)

    verifier = OAuthTokenVerifier(config)
    keyset = jose_provider.parse_jwks(public_jwks(get_signing_key()))
    verifier._jwks = keyset
    verifier._jwks_expires_at = time.monotonic() + 3600.0
    # Only the network hop is stubbed; sign/verify crypto is exercised for real.
    verifier._load_jwks = _mock.AsyncMock(return_value=keyset)

    wrapped = BearerAuthMiddleware(
        app,
        operator_key=_OPERATOR_SECRET,
        oauth_config=config,
        oauth_verifier=verifier,
    )
    return TestClient(wrapped)


def _register(client: TestClient) -> str:
    resp = client.post(
        "/oauth/register",
        json={"redirect_uris": [_CB], "client_name": "E2E Connector"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["client_id"]


def _authorize_get_code(client: TestClient, client_id: str, challenge: str) -> str:
    """Run authorize GET (consent) -> POST approve, return the issued code."""
    resp = client.get(
        "/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": _CB,
            "scope": "menhir:read menhir:write menhir:admin",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "xyz-state",
        },
    )
    assert resp.status_code == 200, resp.text
    fields = _hidden_fields(resp.text)
    assert "consent_token" in fields

    form = dict(fields)
    form["decision"] = "approve"
    form["admin_secret"] = _OPERATOR_SECRET
    approve = client.post("/oauth/authorize", data=form, follow_redirects=False)
    assert approve.status_code == 302, approve.text
    location = approve.headers["location"]
    assert location.startswith(_CB)
    code = re.search(r"[?&]code=([^&]+)", location)
    assert code, location
    return code.group(1)


def _redeem_token(client: TestClient, code: str, client_id: str, verifier: str) -> str:
    resp = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": _CB,
            "client_id": client_id,
            "code_verifier": verifier,
            "resource": _RESOURCE,
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


# ---------------------------------------------------------------------------


def test_full_flow_minted_token_accepted_at_operator_tier():
    client = _build_client()
    verifier, challenge = _pkce()

    client_id = _register(client)
    code = _authorize_get_code(client, client_id, challenge)
    access_token = _redeem_token(client, code, client_id, verifier)

    resp = client.get("/mcp/test", headers={"Authorization": f"Bearer {access_token}"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["auth_mode"] == "oauth"
    # Full-grant client -> menhir:admin -> operator tier.
    assert body["tier"] == "operator"
    assert body["client_id"] == client_id
    assert body["client_name"] == "E2E Connector"
    # Admin-approved subject flows through as the verified user identity.
    assert body["user_id"] == "menhir-admin"


def test_protected_route_rejects_request_without_token():
    client = _build_client()
    resp = client.get("/mcp/test")
    assert resp.status_code == 401
    assert "resource_metadata=" in resp.headers["www-authenticate"]


def test_protected_route_rejects_garbage_token():
    client = _build_client()
    resp = client.get("/mcp/test", headers={"Authorization": "Bearer not-a-real-token"})
    assert resp.status_code == 401
    assert resp.json()["code"] in ("invalid_token", "server_error")


def test_code_is_single_use_across_full_flow():
    client = _build_client()
    verifier, challenge = _pkce()
    client_id = _register(client)
    code = _authorize_get_code(client, client_id, challenge)

    assert _redeem_token(client, code, client_id, verifier)  # first redemption works
    replay = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": _CB,
            "client_id": client_id,
            "code_verifier": verifier,
            "resource": _RESOURCE,
        },
    )
    assert replay.status_code == 400
    assert replay.json()["error"] == "invalid_grant"


def test_read_only_client_maps_to_readonly_tier():
    """A code minted with only menhir:read yields a readonly-tier principal end to end."""
    client = _build_client()
    verifier, challenge = _pkce()
    client_id = _register(client)

    resp = client.get(
        "/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": _CB,
            "scope": "menhir:read",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
    )
    assert resp.status_code == 200
    form = dict(_hidden_fields(resp.text))
    assert form["scope"] == "menhir:read"
    form["decision"] = "approve"
    form["admin_secret"] = _OPERATOR_SECRET
    approve = client.post("/oauth/authorize", data=form, follow_redirects=False)
    code = re.search(r"[?&]code=([^&]+)", approve.headers["location"]).group(1)

    access_token = _redeem_token(client, code, client_id, verifier)
    resp = client.get("/mcp/test", headers={"Authorization": f"Bearer {access_token}"})
    assert resp.status_code == 200
    assert resp.json()["tier"] == "readonly"
