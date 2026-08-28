"""Tests for OAuth URL client_id metadata with DCR fallback.

All resolver/network access is injected; no real network is used.
"""

from __future__ import annotations

import re
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from menhir.api import auth_code_store, oauth_client_store
from menhir.api.oauth_authorize import router as oauth_authorize_router
from menhir.api.oauth_as_register import router as oauth_as_register_router
from menhir.api.oauth_client_store import (
    OAuthClient,
    get_client_store,
    new_client_id,
    record_cimd_fetch,
)

pytestmark = pytest.mark.unit

_URL = "https://client.example.com/.well-known/oauth-client"
_CB = "https://app.example.com/cb"

_ENABLED = SimpleNamespace(
    oauth_as_enabled=True,
    oauth_public_base_url="https://memory.example.com",
    operator_key="s3cret",
)
_ENABLED_REFRESH = SimpleNamespace(
    oauth_as_enabled=True,
    oauth_public_base_url="https://memory.example.com",
    operator_key="s3cret",
    oauth_as_refresh_tokens_enabled=True,
)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    from menhir.api import oauth_authorize

    monkeypatch.setenv("MENHIR_OAUTH_AS_DIR", str(tmp_path))
    monkeypatch.setenv("MENHIR_OAUTH_AS_CONSENT_SECRET", "test-consent-secret")
    monkeypatch.delenv("MENHIR_OPERATOR_KEY", raising=False)
    monkeypatch.setattr(oauth_client_store, "_client_store_singleton", None, raising=False)
    monkeypatch.setattr(auth_code_store, "_auth_code_store_singleton", None, raising=False)
    monkeypatch.setattr(oauth_authorize, "_cimd_resolver", None, raising=False)
    yield
    monkeypatch.setattr(oauth_client_store, "_client_store_singleton", None, raising=False)
    monkeypatch.setattr(auth_code_store, "_auth_code_store_singleton", None, raising=False)


def _doc(url: str = _URL, cb: str = _CB, **extra) -> dict:
    doc = {
        "client_id": url,
        "client_name": "CIMD App",
        "redirect_uris": [cb],
        "token_endpoint_auth_method": "none",
    }
    doc.update(extra)
    return doc


def _install_resolver(monkeypatch, docs: dict[str, dict], calls: list[str] | None = None):
    from menhir.api import oauth_authorize

    async def resolver(url: str):
        if calls is not None:
            calls.append(url)
        if url not in docs:
            raise RuntimeError("no such document")
        return docs[url]

    monkeypatch.setattr(oauth_authorize, "_cimd_resolver", resolver)
    return resolver


def test_agent_smith_cimd_resolves_locally_without_public_hairpin(monkeypatch):
    calls: list[str] = []
    _install_resolver(monkeypatch, {}, calls)
    _, challenge = _pkce()
    client_id = (
        "https://memory.example.com/oauth/client-metadata/agent-smith.json?client=codex"
    )

    response = _client().get(
        "/oauth/authorize",
        params=_get_params(
            client_id,
            challenge=challenge,
            redirect_uri="http://127.0.0.1:43682/oauth/callback",
        ),
    )

    assert response.status_code == 200
    assert calls == []
    stored = get_client_store().get(client_id)
    assert stored is not None
    assert stored.client_name == "Agent Smith - Codex"
    assert stored.redirect_uris[0] == "http://127.0.0.1:43682/oauth/callback"


def _pkce() -> tuple[str, str]:
    import base64
    import hashlib

    verifier = "a" * 64
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    return verifier, challenge


def _client(settings=_ENABLED) -> TestClient:
    app = FastAPI()
    app.state.settings = settings
    app.include_router(oauth_authorize_router)
    app.include_router(oauth_as_register_router)
    return TestClient(app, follow_redirects=False)


def _get_params(client_id: str, *, challenge: str, redirect_uri: str = _CB) -> dict:
    return {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": "xyz",
    }


def _extract_hidden(html_text: str) -> dict[str, str]:
    return dict(re.findall(r'name="([^"]+)" value="([^"]*)"', html_text))


# ---------------------------------------------------------------------------
# Valid CIMD flow
# ---------------------------------------------------------------------------


def test_valid_cimd_flow_renders_consent_and_issues_code(monkeypatch):
    calls: list[str] = []
    _install_resolver(monkeypatch, {_URL: _doc()}, calls)
    c = _client()
    _, challenge = _pkce()

    resp = c.get("/oauth/authorize", params=_get_params(_URL, challenge=challenge))
    assert resp.status_code == 200
    assert "CIMD App" in resp.text
    assert calls == [_URL]

    # Approve end-to-end: the durable row resolves on POST without refetch.
    fields = _extract_hidden(resp.text)
    resp2 = c.post(
        "/oauth/authorize",
        data={**fields, "admin_secret": "s3cret", "decision": "approve"},
    )
    assert resp2.status_code == 302
    query = parse_qs(urlsplit(resp2.headers["location"]).query)
    assert query["code"] and query["state"] == ["xyz"]
    assert calls == [_URL]  # fresh cache served the POST too


def test_cimd_selects_none_when_client_prefers_private_key_jwt(monkeypatch):
    """Match ChatGPT's live CIMD shape while retaining Menhir's public-client profile."""
    _install_resolver(
        monkeypatch,
        {
            _URL: _doc(
                token_endpoint_auth_method="private_key_jwt",
                token_endpoint_auth_methods_supported=["none", "private_key_jwt"],
                token_endpoint_auth_signing_alg="RS256",
                jwks_uri="https://client.example.com/.well-known/jwks.json",
            )
        },
    )
    c = _client()
    _, challenge = _pkce()

    resp = c.get("/oauth/authorize", params=_get_params(_URL, challenge=challenge))

    assert resp.status_code == 200
    assert get_client_store().get(_URL).token_endpoint_auth_method == "none"


def test_cimd_rejects_private_key_jwt_when_none_is_not_offered(monkeypatch):
    _install_resolver(
        monkeypatch,
        {
            _URL: _doc(
                token_endpoint_auth_method="private_key_jwt",
                token_endpoint_auth_methods_supported=["private_key_jwt"],
            )
        },
    )
    c = _client()
    _, challenge = _pkce()

    resp = c.get("/oauth/authorize", params=_get_params(_URL, challenge=challenge))

    assert resp.status_code == 400


def test_cimd_identity_and_redirect_must_match_exactly(monkeypatch):
    _install_resolver(monkeypatch, {"https://other.example.com/c": _doc()})
    c = _client()
    _, challenge = _pkce()

    # Document client_id mismatch -> fail closed, direct 400.
    resp = c.get("/oauth/authorize", params=_get_params(_URL, challenge=challenge))
    assert resp.status_code == 400

    # Exact redirect match required against resolved metadata.
    _install_resolver(monkeypatch, {_URL: _doc(cb="https://app.example.com/other")})
    resp2 = c.get("/oauth/authorize", params=_get_params(_URL, challenge=challenge))
    assert resp2.status_code == 400

    _install_resolver(monkeypatch, {_URL: _doc()})
    resp3 = c.get(
        "/oauth/authorize",
        params=_get_params(_URL, challenge=challenge, redirect_uri="https://app.example.com/cb/"),
    )
    assert resp3.status_code == 400


# ---------------------------------------------------------------------------
# Freshness semantics
# ---------------------------------------------------------------------------


def test_fresh_cache_is_used_without_refetch(monkeypatch):
    calls: list[str] = []
    _install_resolver(monkeypatch, {_URL: _doc(client_name="V1")}, calls)
    c = _client()
    _, challenge = _pkce()

    first = c.get("/oauth/authorize", params=_get_params(_URL, challenge=challenge))
    assert first.status_code == 200

    # Update the served document; a fresh snapshot must NOT be refetched.
    docs = {_URL: _doc(client_name="V2")}
    async def updated(url: str):
        calls.append(url)
        return docs[url]
    from menhir.api import oauth_authorize

    monkeypatch.setattr(oauth_authorize, "_cimd_resolver", updated)

    second = c.get("/oauth/authorize", params=_get_params(_URL, challenge=challenge))
    assert second.status_code == 200
    assert "V1" in second.text
    assert calls.count(_URL) == 1


def test_stale_snapshot_revalidates_and_picks_up_changes(monkeypatch):
    calls: list[str] = []
    _install_resolver(monkeypatch, {_URL: _doc(client_name="V1")}, calls)
    c = _client()
    _, challenge = _pkce()
    assert c.get("/oauth/authorize", params=_get_params(_URL, challenge=challenge)).status_code == 200

    record_cimd_fetch(_URL, now=time.time() - 100000)

    _install_resolver(monkeypatch, {_URL: _doc(client_name="V2-Updated")}, calls)
    resp = c.get("/oauth/authorize", params=_get_params(_URL, challenge=challenge))
    assert resp.status_code == 200
    assert "V2-Updated" in resp.text
    assert calls.count(_URL) == 2


def test_stale_revalidation_failure_fails_closed(monkeypatch):
    calls: list[str] = []
    _install_resolver(monkeypatch, {_URL: _doc()}, calls)
    c = _client()
    _, challenge = _pkce()
    assert c.get("/oauth/authorize", params=_get_params(_URL, challenge=challenge)).status_code == 200

    record_cimd_fetch(_URL, now=time.time() - 100000)

    async def failing(url: str):
        calls.append(url)
        raise OSError("resolver down")
    from menhir.api import oauth_authorize

    monkeypatch.setattr(oauth_authorize, "_cimd_resolver", failing)
    resp = c.get("/oauth/authorize", params=_get_params(_URL, challenge=challenge))
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Persistence / restart reconstruction
# ---------------------------------------------------------------------------


def test_durable_snapshot_survives_restart_without_network(monkeypatch):
    calls: list[str] = []
    _install_resolver(monkeypatch, {_URL: _doc()}, calls)
    c = _client()
    _, challenge = _pkce()
    assert c.get("/oauth/authorize", params=_get_params(_URL, challenge=challenge)).status_code == 200

    # Simulate a restart: drop the store singleton (same DB path) and make any
    # network touch fail loudly.
    monkeypatch.setattr(oauth_client_store, "_client_store_singleton", None, raising=False)
    async def no_network(url: str):
        raise AssertionError("no network after restart")
    from menhir.api import oauth_authorize

    monkeypatch.setattr(oauth_authorize, "_cimd_resolver", no_network)
    resp = c.get("/oauth/authorize", params=_get_params(_URL, challenge=challenge))
    assert resp.status_code == 200
    assert "CIMD App" in resp.text


def test_resolver_failure_for_unknown_url_fails_closed(monkeypatch, caplog):
    calls: list[str] = []

    async def resolver(url: str):
        calls.append(url)
        raise RuntimeError("private DNS answer 10.23.45.67 and TLS diagnostic")

    from menhir.api import oauth_authorize

    monkeypatch.setattr(oauth_authorize, "_cimd_resolver", resolver)
    c = _client()
    _, challenge = _pkce()
    resp = c.get("/oauth/authorize", params=_get_params(_URL, challenge=challenge))
    assert resp.status_code == 400
    assert calls == [_URL]
    assert "private DNS answer" not in resp.text
    assert "10.23.45.67" not in resp.text
    assert "private DNS answer" not in caplog.text
    assert "10.23.45.67" not in caplog.text
    assert "CIMD document could not be retrieved or validated" in resp.text


# ---------------------------------------------------------------------------
# DCR fallback + refresh truthfulness
# ---------------------------------------------------------------------------


def test_ordinary_dcr_client_still_works_without_cimd(monkeypatch):
    async def forbidden(url: str):
        raise AssertionError("DCR clients must not hit CIMD")
    from menhir.api import oauth_authorize

    monkeypatch.setattr(oauth_authorize, "_cimd_resolver", forbidden)

    client_id = new_client_id()
    get_client_store().register(
        OAuthClient(
            client_id=client_id,
            client_name="Legacy DCR",
            redirect_uris=(_CB,),
            scopes=("menhir:read",),
            client_secret_hash="",
            created_at=0.0,
            token_endpoint_auth_method="none",
        )
    )
    c = _client()
    _, challenge = _pkce()
    resp = c.get("/oauth/authorize", params=_get_params(client_id, challenge=challenge))
    assert resp.status_code == 200
    assert "Legacy DCR" in resp.text


def test_dcr_rejects_refresh_grant_when_disabled():
    c = _client(_ENABLED)
    resp = c.post(
        "/oauth/register",
        json={"redirect_uris": [_CB], "grant_types": ["authorization_code", "refresh_token"]},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"] == "invalid_client_metadata"
    assert "refresh_token" in body["error_description"]

    ok = c.post("/oauth/register", json={"redirect_uris": [_CB]})
    assert ok.status_code == 201
    assert ok.json()["grant_types"] == ["authorization_code"]
    assert "offline_access" not in ok.json()["scope"]


def test_dcr_accepts_refresh_grant_and_advertises_offline_access_when_enabled():
    c = _client(_ENABLED_REFRESH)
    resp = c.post(
        "/oauth/register",
        json={
            "redirect_uris": [_CB],
            "grant_types": ["authorization_code", "refresh_token"],
            "scope": "menhir:read offline_access",
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["grant_types"] == ["authorization_code", "refresh_token"]
    assert "offline_access" in body["scope"]


def test_cimd_clients_get_full_scope_surface_only_when_refresh_enabled(monkeypatch):
    _install_resolver(monkeypatch, {_URL: _doc()})
    c = _client()
    _, challenge = _pkce()
    resp = c.get(
        "/oauth/authorize",
        params=_get_params(_URL, challenge=challenge),
    )
    assert resp.status_code == 200
    fields = _extract_hidden(resp.text)
    assert "offline_access" not in fields["scope"]

    # Requesting a scope outside the granted subset is rejected via redirect.
    bad = c.get(
        "/oauth/authorize",
        params={**_get_params(_URL, challenge=challenge), "scope": "not:a:scope"},
    )
    assert bad.status_code == 302
    assert parse_qs(urlsplit(bad.headers["location"]).query)["error"] == ["invalid_scope"]

    record_cimd_fetch(_URL, now=time.time() - 100000)
    oauth_client_store._client_store_singleton = None
    _install_resolver(monkeypatch, {_URL: _doc()})
    refresh_client = _client(_ENABLED_REFRESH)
    refresh_response = refresh_client.get(
        "/oauth/authorize",
        params={
            **_get_params(_URL, challenge=challenge),
            "scope": "menhir:read menhir:write menhir:admin offline_access",
        },
    )
    assert refresh_response.status_code == 200
    refreshed = get_client_store().get(_URL)
    assert refreshed is not None
    assert set(refreshed.scopes) == {
        "menhir:read",
        "menhir:write",
        "menhir:admin",
        "offline_access",
    }
