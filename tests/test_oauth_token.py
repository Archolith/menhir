"""Tests for the /oauth/token endpoint (Phase 7): auth-code -> signed RS256 JWT."""

from __future__ import annotations

import base64
import hashlib
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from menhir.api import auth_code_store, jose_provider, oauth_client_store, oauth_keys
from menhir.api.auth_code_store import get_auth_code_store
from menhir.api.oauth_client_store import OAuthClient, get_client_store, new_client_id
from menhir.api.oauth_keys import get_signing_key, public_jwks
from menhir.api.oauth_token import router as oauth_token_router

pytestmark = pytest.mark.unit

_BASE = "https://memory.example.com"
_ENABLED = SimpleNamespace(oauth_as_enabled=True, oauth_public_base_url=_BASE)
_DISABLED = SimpleNamespace(oauth_as_enabled=False)
_CB = "https://app.example.com/cb"
_SCOPE = "menhir:read menhir:write menhir:admin"
# Canonical resource derived from _BASE the same way build_oauth_config derives it.
# The token endpoint requires `resource`, and the exchange rejects a code whose bound
# resource does not match the one presented, so both sides must carry it.
_RESOURCE = f"{_BASE}/mcp-http"


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
    verifier = "a" * 64
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    return verifier, challenge


def _register_client() -> str:
    client_id = new_client_id()
    get_client_store().register(
        OAuthClient(
            client_id=client_id,
            client_name="Test App",
            redirect_uris=(_CB,),
            scopes=("menhir:read", "menhir:write", "menhir:admin"),
            client_secret_hash="",
            created_at=0.0,
            token_endpoint_auth_method="none",
        )
    )
    return client_id


def _seed_code(cid: str, challenge: str, *, redirect_uri: str = _CB, scope: str = _SCOPE) -> str:
    return get_auth_code_store().issue(
        client_id=cid,
        redirect_uri=redirect_uri,
        scope=scope,
        code_challenge=challenge,
        code_challenge_method="S256",
        resource=_RESOURCE,
        subject="menhir-admin",
    )


def _client(settings=_ENABLED) -> TestClient:
    app = FastAPI()
    app.state.settings = settings
    app.include_router(oauth_token_router)
    return TestClient(app)


def _token_form(code: str, cid: str, verifier: str, *, redirect_uri: str = _CB) -> dict[str, str]:
    return {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": cid,
        "code_verifier": verifier,
        "resource": _RESOURCE,
    }


# ---------------------------------------------------------------------------


def test_disabled_returns_404():
    assert _client(_DISABLED).post("/oauth/token", data={}).status_code == 404


def test_unsupported_grant_type():
    resp = _client().post("/oauth/token", data={"grant_type": "password"})
    assert resp.status_code == 400
    assert resp.json()["error"] == "unsupported_grant_type"


def test_missing_code_verifier_invalid_request():
    verifier, challenge = _pkce()
    cid = _register_client()
    code = _seed_code(cid, challenge)
    form = _token_form(code, cid, verifier)
    del form["code_verifier"]
    resp = _client().post("/oauth/token", data=form)
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_request"


def test_unknown_code_invalid_grant():
    verifier, _ = _pkce()
    cid = _register_client()
    resp = _client().post("/oauth/token", data=_token_form("no-such-code", cid, verifier))
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_grant"


def test_wrong_verifier_fails_pkce_and_burns_code():
    _, challenge = _pkce()
    cid = _register_client()
    code = _seed_code(cid, challenge)
    c = _client()
    resp = c.post("/oauth/token", data=_token_form(code, cid, "b" * 64))
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_grant"
    # Code was burned by redeem even though PKCE failed: a correct retry also fails.
    resp2 = c.post("/oauth/token", data=_token_form(code, cid, "a" * 64))
    assert resp2.status_code == 400
    assert resp2.json()["error"] == "invalid_grant"


def test_happy_path_returns_bearer_token():
    verifier, challenge = _pkce()
    cid = _register_client()
    code = _seed_code(cid, challenge)
    resp = _client().post("/oauth/token", data=_token_form(code, cid, verifier))
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    body = resp.json()
    assert body["token_type"] == "Bearer"
    assert body["expires_in"] == 3600
    assert body["scope"] == _SCOPE
    assert isinstance(body["access_token"], str) and body["access_token"]


def test_minted_jwt_verifies_through_seam():
    verifier, challenge = _pkce()
    cid = _register_client()
    code = _seed_code(cid, challenge)
    resp = _client().post("/oauth/token", data=_token_form(code, cid, verifier))
    token = resp.json()["access_token"]

    keyset = jose_provider.parse_jwks(public_jwks(get_signing_key()))
    claims = jose_provider.verify_jwt(token, keyset, ["RS256"], 60)
    assert claims["iss"] == _BASE
    assert claims["aud"] == f"{_BASE}/mcp-http"
    assert claims["sub"] == "menhir-admin"
    assert claims["scope"] == _SCOPE
    assert claims["client_id"] == cid
    assert claims["client_name"] == "Test App"
    assert claims["exp"] > claims["iat"]


def test_code_is_single_use():
    verifier, challenge = _pkce()
    cid = _register_client()
    code = _seed_code(cid, challenge)
    c = _client()
    assert c.post("/oauth/token", data=_token_form(code, cid, verifier)).status_code == 200
    resp2 = c.post("/oauth/token", data=_token_form(code, cid, verifier))
    assert resp2.status_code == 400
    assert resp2.json()["error"] == "invalid_grant"


def test_wrong_redirect_uri_invalid_grant():
    verifier, challenge = _pkce()
    cid = _register_client()
    code = _seed_code(cid, challenge)
    resp = _client().post(
        "/oauth/token",
        data=_token_form(code, cid, verifier, redirect_uri="https://app.example.com/other"),
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_grant"
