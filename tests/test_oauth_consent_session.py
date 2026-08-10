"""Tests for the Phase 8 consent-session cookie (true one-click after first approval)."""

from __future__ import annotations

import base64
import hashlib
import re
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from menhir.api import auth_code_store, oauth_authorize, oauth_client_store
from menhir.api.auth_code_store import get_auth_code_store
from menhir.api.oauth_authorize import router as oauth_authorize_router
from menhir.api.oauth_client_store import OAuthClient, get_client_store, new_client_id

pytestmark = pytest.mark.unit

_CB = "https://app.example.com/cb"
_ENABLED = SimpleNamespace(
    oauth_as_enabled=True,
    oauth_public_base_url="https://memory.example.com",
    operator_key="s3cret",
)
_NO_OPERATOR = SimpleNamespace(
    oauth_as_enabled=True,
    oauth_public_base_url="https://memory.example.com",
    operator_key="",
)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("MENHIR_OAUTH_AS_DIR", str(tmp_path))
    monkeypatch.setenv("MENHIR_OAUTH_AS_CONSENT_SECRET", "test-consent-secret")
    # Hermeticity: clear any operator key leaking in from the repo .env, so a
    # settings double with operator_key="" is authoritative (see api/oauth._get_setting,
    # which otherwise falls through to os.getenv("MENHIR_OPERATOR_KEY")).
    monkeypatch.delenv("MENHIR_OPERATOR_KEY", raising=False)
    monkeypatch.setattr(oauth_client_store, "_client_store_singleton", None, raising=False)
    monkeypatch.setattr(auth_code_store, "_auth_code_store_singleton", None, raising=False)
    yield
    monkeypatch.setattr(oauth_client_store, "_client_store_singleton", None, raising=False)
    monkeypatch.setattr(auth_code_store, "_auth_code_store_singleton", None, raising=False)


def _pkce() -> tuple[str, str]:
    verifier = "a" * 64
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    return verifier, challenge


def _register_client(redirect_uris=(_CB,)) -> str:
    client_id = new_client_id()
    get_client_store().register(
        OAuthClient(
            client_id=client_id,
            client_name="Test App",
            redirect_uris=tuple(redirect_uris),
            scopes=("menhir:read", "menhir:write", "menhir:admin"),
            client_secret_hash="",
            created_at=0.0,
            token_endpoint_auth_method="none",
        )
    )
    return client_id


def _client(settings=_ENABLED) -> TestClient:
    app = FastAPI()
    app.state.settings = settings
    app.include_router(oauth_authorize_router)
    return TestClient(app, follow_redirects=False)


def _params(cid: str, challenge: str, *, redirect_uri: str = _CB, state: str = "xyz-state"):
    return {
        "client_id": cid,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }


def _extract_hidden(html_text: str) -> dict[str, str]:
    return dict(re.findall(r'name="([^"]+)" value="([^"]*)"', html_text))


def _location_query(resp) -> dict[str, list[str]]:
    return parse_qs(urlsplit(resp.headers["location"]).query)


def _approve(c: TestClient, cid: str, challenge: str):
    resp = c.get("/oauth/authorize", params=_params(cid, challenge))
    form = _extract_hidden(resp.text)
    form["admin_secret"] = "s3cret"
    form["decision"] = "approve"
    return c.post("/oauth/authorize", data=form)


def _get_with_session(c: TestClient, params: dict, token: str):
    c.cookies.set("menhir_as_session", token)
    return c.get("/oauth/authorize", params=params)


def _session_cookie_value(resp) -> str:
    match = re.search(r"menhir_as_session=([^;]+)", resp.headers["set-cookie"])
    assert match is not None, "no menhir_as_session Set-Cookie on response"
    return match.group(1)


# ---------------------------------------------------------------------------


def test_approve_sets_httponly_session_cookie():
    _, challenge = _pkce()
    cid = _register_client()
    resp = _approve(_client(), cid, challenge)
    assert resp.status_code == 302
    set_cookie = resp.headers["set-cookie"]
    assert "menhir_as_session=" in set_cookie
    assert "httponly" in set_cookie.lower()


def test_valid_session_cookie_one_clicks():
    verifier, challenge = _pkce()
    cid = _register_client()
    token = oauth_authorize._sign_session("menhir-admin", (cid,))
    resp = _get_with_session(_client(), _params(cid, challenge), token)
    assert resp.status_code == 302
    q = _location_query(resp)
    assert q["state"] == ["xyz-state"]
    record = get_auth_code_store().redeem(code=q["code"][0], client_id=cid, redirect_uri=_CB)
    assert record is not None
    assert record.subject == "menhir-admin"
    assert record.code_challenge == challenge


def test_garbage_cookie_falls_through_to_consent():
    _, challenge = _pkce()
    cid = _register_client()
    resp = _get_with_session(_client(), _params(cid, challenge), "garbage.sig")
    assert resp.status_code == 200
    assert 'name="consent_token"' in resp.text


def test_expired_cookie_falls_through_to_consent(monkeypatch):
    _, challenge = _pkce()
    cid = _register_client()
    monkeypatch.setenv("MENHIR_OAUTH_AS_SESSION_TTL_S", "-1")
    token = oauth_authorize._sign_session("menhir-admin", (cid,))
    resp = _get_with_session(_client(), _params(cid, challenge), token)
    assert resp.status_code == 200
    assert 'name="consent_token"' in resp.text


def test_one_click_still_validates_unknown_client():
    _, challenge = _pkce()
    token = oauth_authorize._sign_session("menhir-admin", ("nonexistent",))
    resp = _get_with_session(_client(), _params("nonexistent", challenge), token)
    assert resp.status_code == 400
    assert "location" not in resp.headers


def test_one_click_still_validates_redirect_uri():
    _, challenge = _pkce()
    cid = _register_client()
    token = oauth_authorize._sign_session("menhir-admin", (cid,))
    params = _params(cid, challenge, redirect_uri="https://evil.example.com/cb")
    resp = _get_with_session(_client(), params, token)
    assert resp.status_code == 400
    assert "location" not in resp.headers


def test_no_operator_key_disables_one_click():
    _, challenge = _pkce()
    cid = _register_client()
    token = oauth_authorize._sign_session("menhir-admin", (cid,))
    resp = _get_with_session(_client(_NO_OPERATOR), _params(cid, challenge), token)
    assert resp.status_code == 200
    assert 'name="consent_token"' in resp.text


# --- AS-001 regression: client-scoped one-click + SameSite=Strict ----------


def test_one_click_denied_for_unapproved_client():
    """A live session bound to cid_a must NOT one-click a different validated client
    cid_b (incl. an attacker-registered one) — it falls through to the consent page."""
    _, challenge = _pkce()
    cid_a = _register_client()
    cid_b = _register_client()
    token = oauth_authorize._sign_session("menhir-admin", (cid_a,))
    resp = _get_with_session(_client(), _params(cid_b, challenge), token)
    assert resp.status_code == 200
    assert 'name="consent_token"' in resp.text


def test_session_cookie_is_samesite_strict():
    _, challenge = _pkce()
    cid = _register_client()
    resp = _approve(_client(), cid, challenge)
    assert resp.status_code == 302
    assert "samesite=strict" in resp.headers["set-cookie"].lower()


def test_approve_then_reconnect_same_client_one_clicks():
    _, challenge = _pkce()
    cid = _register_client()
    c = _client()
    approve_resp = _approve(c, cid, challenge)
    assert approve_resp.status_code == 302
    token = _session_cookie_value(approve_resp)
    resp = _get_with_session(_client(), _params(cid, challenge), token)
    assert resp.status_code == 302
    q = _location_query(resp)
    assert "code" in q


def test_approve_accumulates_two_clients():
    _, challenge = _pkce()
    cid_a = _register_client()
    cid_b = _register_client()
    c = _client()
    first = _approve(c, cid_a, challenge)
    c.cookies.set("menhir_as_session", _session_cookie_value(first))
    second = _approve(c, cid_b, challenge)
    token = _session_cookie_value(second)
    for cid in (cid_a, cid_b):
        resp = _get_with_session(_client(), _params(cid, challenge), token)
        assert resp.status_code == 302, cid
        assert "code" in _location_query(resp)
