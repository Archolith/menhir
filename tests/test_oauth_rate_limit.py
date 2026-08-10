"""Tests for embedded OAuth AS abuse hardening (AS-002 / AS-004):
the in-process fixed-window rate limiter, DCR throttle + client ceiling, the
failed-approve throttle, and single-use consent tokens."""

from __future__ import annotations

import base64
import hashlib
import re
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from menhir.api import auth_code_store, oauth_as_register, oauth_authorize, oauth_client_store
from menhir.api.oauth_as_register import router as oauth_as_register_router
from menhir.api.oauth_authorize import router as oauth_authorize_router
from menhir.api.oauth_client_store import OAuthClient, get_client_store, new_client_id
from menhir.api.oauth_rate_limit import (
    FixedWindowLimiter,
    build_approve_limiter,
    build_register_limiter,
    client_ip,
)

pytestmark = pytest.mark.unit

_CB = "https://app.example.com/cb"
_ENABLED = SimpleNamespace(
    oauth_as_enabled=True,
    oauth_public_base_url="https://memory.example.com",
    operator_key="s3cret",
)


# ---------------------------------------------------------------------------
# FixedWindowLimiter unit tests
# ---------------------------------------------------------------------------


def test_allows_up_to_max_then_blocks():
    limiter = FixedWindowLimiter(max_per_window=3, window_s=600)
    assert [limiter.allow("k") for _ in range(5)] == [True, True, True, False, False]


def test_separate_keys_are_independent():
    limiter = FixedWindowLimiter(max_per_window=1, window_s=600)
    assert limiter.allow("a") is True
    assert limiter.allow("b") is True
    assert limiter.allow("a") is False


def test_window_reset_restores_budget(monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(oauth_authorize.time, "monotonic", lambda: clock["t"], raising=False)
    from menhir.api import oauth_rate_limit

    monkeypatch.setattr(oauth_rate_limit.time, "monotonic", lambda: clock["t"])
    limiter = FixedWindowLimiter(max_per_window=1, window_s=10)
    assert limiter.allow("k") is True
    assert limiter.allow("k") is False
    clock["t"] += 11  # advance past the window boundary
    assert limiter.allow("k") is True


def test_client_ip_reads_peer_host():
    req = SimpleNamespace(client=SimpleNamespace(host="203.0.113.7"))
    assert client_ip(req) == "203.0.113.7"


def test_client_ip_defaults_when_unknown():
    assert client_ip(SimpleNamespace(client=None)) == "unknown"
    assert client_ip(SimpleNamespace()) == "unknown"


def test_client_ip_ignores_forwarded_by_default(monkeypatch):
    """RL-001: X-Forwarded-For is NOT trusted unless MENHIR_TRUSTED_PROXY is set."""
    monkeypatch.delenv("MENHIR_TRUSTED_PROXY", raising=False)
    req = SimpleNamespace(
        client=SimpleNamespace(host="127.0.0.1"),
        headers={"x-forwarded-for": "203.0.113.7, 198.51.100.9"},
    )
    assert client_ip(req) == "127.0.0.1"


def test_client_ip_uses_forwarded_last_hop_when_trusted(monkeypatch):
    """RL-001: with a trusted proxy, key on the LAST XFF hop (the IP the proxy saw)."""
    monkeypatch.setenv("MENHIR_TRUSTED_PROXY", "1")
    req = SimpleNamespace(
        client=SimpleNamespace(host="127.0.0.1"),
        headers={"x-forwarded-for": "203.0.113.7, 198.51.100.9"},
    )
    assert client_ip(req) == "198.51.100.9"


def test_client_ip_trusted_proxy_falls_back_to_peer_without_header(monkeypatch):
    monkeypatch.setenv("MENHIR_TRUSTED_PROXY", "1")
    req = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), headers={})
    assert client_ip(req) == "127.0.0.1"


def test_limiter_bounds_tracked_keys_under_distinct_flood(monkeypatch):
    """RL-002: the tracked-key set stays bounded under a distinct-key flood."""
    from menhir.api import oauth_rate_limit

    monkeypatch.setattr(oauth_rate_limit, "_MAX_TRACKED_KEYS", 8)
    limiter = FixedWindowLimiter(max_per_window=1, window_s=600)
    for i in range(200):
        limiter.allow(f"ip-{i}")
    assert len(limiter._state) <= 8


def test_env_builders_read_overrides(monkeypatch):
    monkeypatch.setenv("MENHIR_OAUTH_AS_REGISTER_RATE", "2")
    monkeypatch.setenv("MENHIR_OAUTH_AS_APPROVE_RATE", "1")
    reg = build_register_limiter()
    app = build_approve_limiter()
    assert [reg.allow("x") for _ in range(3)] == [True, True, False]
    assert [app.allow("y") for _ in range(2)] == [True, False]


# ---------------------------------------------------------------------------
# DCR (/oauth/register) throttle + ceiling
# ---------------------------------------------------------------------------


@pytest.fixture
def _register_isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("MENHIR_OAUTH_AS_DIR", str(tmp_path))
    monkeypatch.setattr(oauth_client_store, "_client_store_singleton", None, raising=False)
    yield
    monkeypatch.setattr(oauth_client_store, "_client_store_singleton", None, raising=False)


def _register_client_app() -> TestClient:
    app = FastAPI()
    app.state.settings = _ENABLED
    app.include_router(oauth_as_register_router)
    return TestClient(app)


def test_register_rate_limited(_register_isolate):
    oauth_as_register._register_limiter = FixedWindowLimiter(max_per_window=2, window_s=600)
    c = _register_client_app()
    body = {"redirect_uris": [_CB], "client_name": "App"}
    assert c.post("/oauth/register", json=body).status_code == 201
    assert c.post("/oauth/register", json=body).status_code == 201
    over = c.post("/oauth/register", json=body)
    assert over.status_code == 429
    assert over.json()["error"] == "temporarily_unavailable"


def test_register_ceiling_refuses_new_clients(_register_isolate, monkeypatch):
    monkeypatch.setenv("MENHIR_OAUTH_AS_MAX_CLIENTS", "2")
    c = _register_client_app()
    body = {"redirect_uris": [_CB], "client_name": "App"}
    assert c.post("/oauth/register", json=body).status_code == 201
    assert c.post("/oauth/register", json=body).status_code == 201
    over = c.post("/oauth/register", json=body)
    assert over.status_code == 429
    assert over.json()["error"] == "temporarily_unavailable"


# ---------------------------------------------------------------------------
# /oauth/authorize POST throttle + single-use consent token
# ---------------------------------------------------------------------------


@pytest.fixture
def _authorize_isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("MENHIR_OAUTH_AS_DIR", str(tmp_path))
    monkeypatch.setenv("MENHIR_OAUTH_AS_CONSENT_SECRET", "test-consent-secret")
    monkeypatch.setattr(oauth_client_store, "_client_store_singleton", None, raising=False)
    monkeypatch.setattr(auth_code_store, "_auth_code_store_singleton", None, raising=False)
    yield
    monkeypatch.setattr(oauth_client_store, "_client_store_singleton", None, raising=False)
    monkeypatch.setattr(auth_code_store, "_auth_code_store_singleton", None, raising=False)


def _pkce() -> tuple[str, str]:
    verifier = "a" * 64
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    return verifier, challenge


def _register() -> str:
    cid = new_client_id()
    get_client_store().register(
        OAuthClient(
            client_id=cid,
            client_name="Test App",
            redirect_uris=(_CB,),
            scopes=("menhir:read", "menhir:write", "menhir:admin"),
            client_secret_hash="",
            created_at=0.0,
            token_endpoint_auth_method="none",
        )
    )
    return cid


def _authorize_app() -> TestClient:
    app = FastAPI()
    app.state.settings = _ENABLED
    app.include_router(oauth_authorize_router)
    return TestClient(app, follow_redirects=False)


def _params(cid: str, challenge: str) -> dict:
    return {
        "client_id": cid,
        "redirect_uri": _CB,
        "response_type": "code",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": "xyz-state",
    }


def _hidden(html_text: str) -> dict[str, str]:
    return dict(re.findall(r'name="([^"]+)" value="([^"]*)"', html_text))


def _consent_form(c: TestClient, cid: str, challenge: str, *, secret: str = "s3cret") -> dict:
    resp = c.get("/oauth/authorize", params=_params(cid, challenge))
    form = _hidden(resp.text)
    form["admin_secret"] = secret
    form["decision"] = "approve"
    return form


def test_authorize_post_rate_limited(_authorize_isolate):
    oauth_authorize._approve_limiter = FixedWindowLimiter(max_per_window=1, window_s=600)
    _, challenge = _pkce()
    cid = _register()
    c = _authorize_app()
    # First bad-secret attempt reaches the secret check (401).
    first = c.post("/oauth/authorize", data=_consent_form(c, cid, challenge, secret="wrong"))
    assert first.status_code == 401
    # Second attempt (fresh consent token) is throttled before the secret is evaluated.
    second = c.post("/oauth/authorize", data=_consent_form(c, cid, challenge, secret="wrong"))
    assert second.status_code == 429


def test_consent_token_single_use(_authorize_isolate):
    _, challenge = _pkce()
    cid = _register()
    c = _authorize_app()
    form = _consent_form(c, cid, challenge)
    ok = c.post("/oauth/authorize", data=form)
    assert ok.status_code == 302
    assert "code" in parse_qs(urlsplit(ok.headers["location"]).query)
    # Replaying the exact same consent token (the brute-force amplifier) is rejected.
    replay = c.post("/oauth/authorize", data=form)
    assert replay.status_code == 400
    assert "already been used" in replay.text
