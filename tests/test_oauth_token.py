"""Tests for the /oauth/token endpoint (Phase 7): auth-code -> signed RS256 JWT."""

from __future__ import annotations

import base64
import hashlib
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from menhir.api import (
    auth_code_store,
    jose_provider,
    oauth_client_store,
    oauth_keys,
    oauth_refresh_store,
)
from menhir.api.auth_code_store import get_auth_code_store
from menhir.api.oauth_client_store import OAuthClient, get_client_store, new_client_id
from menhir.api.oauth_keys import get_signing_key, public_jwks
from menhir.api.oauth_token import router as oauth_token_router

pytestmark = pytest.mark.unit

_BASE = "https://memory.example.com"
_ENABLED = SimpleNamespace(oauth_as_enabled=True, oauth_public_base_url=_BASE)
_ENABLED_REFRESH = SimpleNamespace(
    oauth_as_enabled=True,
    oauth_public_base_url=_BASE,
    oauth_as_refresh_tokens_enabled=True,
    oauth_as_refresh_ttl_s=2592000,
)
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
    monkeypatch.setattr(oauth_refresh_store, "_refresh_store_singleton", None, raising=False)
    yield
    monkeypatch.setattr(oauth_client_store, "_client_store_singleton", None, raising=False)
    monkeypatch.setattr(auth_code_store, "_auth_code_store_singleton", None, raising=False)
    monkeypatch.setattr(oauth_keys, "_SIGNING_KEY", None, raising=False)
    monkeypatch.setattr(oauth_refresh_store, "_refresh_store_singleton", None, raising=False)


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
    app.state.oauth_refresh_store = (
        oauth_refresh_store.configure_refresh_store(settings)
        if getattr(settings, "oauth_as_refresh_tokens_enabled", False)
        else None
    )
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


def _issue_initial_refresh() -> tuple[TestClient, str, dict[str, object]]:
    verifier, challenge = _pkce()
    cid = _register_client()
    code = _seed_code(cid, challenge, scope=f"{_SCOPE} offline_access")
    client = _client(_ENABLED_REFRESH)
    response = client.post("/oauth/token", data=_token_form(code, cid, verifier))
    assert response.status_code == 200
    return client, cid, response.json()


def _refresh_form(
    token: str,
    cid: str,
    *,
    resource: str = _RESOURCE,
    scope: str | None = None,
) -> dict[str, str]:
    form = {
        "grant_type": "refresh_token",
        "refresh_token": token,
        "client_id": cid,
        "resource": resource,
    }
    if scope is not None:
        form["scope"] = scope
    return form


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


def test_refresh_disabled_rejects_refresh_grant_with_no_store_headers():
    response = _client().post(
        "/oauth/token",
        data=_refresh_form("not-a-token", "client-a"),
    )

    assert response.status_code == 400
    assert response.json()["error"] == "unsupported_grant_type"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"


def test_offline_access_code_exchange_returns_refresh_token():
    _client_instance, _cid, body = _issue_initial_refresh()

    assert body["refresh_token"]
    assert set(str(body["scope"]).split()) == {
        "menhir:read",
        "menhir:write",
        "menhir:admin",
        "offline_access",
    }


def test_refresh_rotates_once_and_replay_revokes_family():
    client, cid, initial = _issue_initial_refresh()
    rotated = client.post(
        "/oauth/token",
        data=_refresh_form(str(initial["refresh_token"]), cid),
    )

    assert rotated.status_code == 200
    assert rotated.headers["cache-control"] == "no-store"
    assert rotated.headers["pragma"] == "no-cache"
    replacement = rotated.json()["refresh_token"]
    assert replacement != initial["refresh_token"]

    replay = client.post(
        "/oauth/token",
        data=_refresh_form(str(initial["refresh_token"]), cid),
    )
    assert replay.status_code == 400
    assert replay.json()["error"] == "invalid_grant"

    revoked_replacement = client.post(
        "/oauth/token",
        data=_refresh_form(str(replacement), cid),
    )
    assert revoked_replacement.status_code == 400
    assert revoked_replacement.json()["error"] == "invalid_grant"


def test_refresh_scope_narrowing_and_expansion_failure():
    client, cid, initial = _issue_initial_refresh()
    expanded = client.post(
        "/oauth/token",
        data=_refresh_form(
            str(initial["refresh_token"]),
            cid,
            scope=f"{_SCOPE} offline_access menhir:future",
        ),
    )
    assert expanded.status_code == 400
    assert expanded.json()["error"] == "invalid_scope"

    narrowed = client.post(
        "/oauth/token",
        data=_refresh_form(
            str(initial["refresh_token"]),
            cid,
            scope="menhir:read offline_access",
        ),
    )
    assert narrowed.status_code == 200
    assert set(narrowed.json()["scope"].split()) == {
        "menhir:read",
        "offline_access",
    }


@pytest.mark.parametrize(
    "overrides",
    [
        {"client_id": "wrong-client"},
        {"resource": "https://memory.example.com/wrong"},
    ],
)
def test_refresh_rejects_wrong_client_or_resource(overrides):
    client, cid, initial = _issue_initial_refresh()
    form = _refresh_form(str(initial["refresh_token"]), cid)
    form.update(overrides)

    response = client.post("/oauth/token", data=form)

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_grant"


def test_refresh_survives_store_reconstruction():
    _client_instance, cid, initial = _issue_initial_refresh()
    oauth_refresh_store._refresh_store_singleton = None
    reconstructed_client = _client(_ENABLED_REFRESH)

    response = reconstructed_client.post(
        "/oauth/token",
        data=_refresh_form(str(initial["refresh_token"]), cid),
    )

    assert response.status_code == 200
    assert response.json()["refresh_token"]
