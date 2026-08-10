"""Tests for the Dynamic Client Registration endpoint (Phase 4, RFC 7591)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from menhir.api import oauth_client_store
from menhir.api.oauth_as_register import router as oauth_as_register_router

pytestmark = pytest.mark.unit

_ENABLED = SimpleNamespace(oauth_as_enabled=True, oauth_public_base_url="https://memory.example.com")


@pytest.fixture(autouse=True)
def _isolate_client_store(tmp_path, monkeypatch):
    # Point the client-store singleton at a throwaway dir and reset it per test.
    monkeypatch.setenv("MENHIR_OAUTH_AS_DIR", str(tmp_path))
    monkeypatch.setattr(oauth_client_store, "_client_store_singleton", None, raising=False)
    yield
    monkeypatch.setattr(oauth_client_store, "_client_store_singleton", None, raising=False)


def _client(settings) -> TestClient:
    app = FastAPI()
    app.state.settings = settings
    app.include_router(oauth_as_register_router)
    return TestClient(app)


def test_disabled_returns_404():
    resp = _client(SimpleNamespace(oauth_as_enabled=False)).post(
        "/oauth/register",
        json={"redirect_uris": ["https://app.example.com/cb"]},
    )
    assert resp.status_code == 404


def test_happy_path_registers_public_client():
    client = _client(_ENABLED)
    resp = client.post(
        "/oauth/register",
        json={"redirect_uris": ["https://app.example.com/cb"], "client_name": "Test App"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert isinstance(body["client_id"], str) and body["client_id"]
    assert "client_secret" not in body
    assert body["token_endpoint_auth_method"] == "none"
    assert body["redirect_uris"] == ["https://app.example.com/cb"]
    assert body["grant_types"] == ["authorization_code"]
    assert isinstance(body["client_id_issued_at"], int)

    # Verify client is durable in the store
    from menhir.api.oauth_client_store import get_client_store

    stored = get_client_store().get(body["client_id"])
    assert stored is not None
    assert stored.client_name == "Test App"


def test_missing_redirect_uris_returns_400():
    resp = _client(_ENABLED).post("/oauth/register", json={})
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_client_metadata"


def test_empty_redirect_uris_returns_400():
    resp = _client(_ENABLED).post("/oauth/register", json={"redirect_uris": []})
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_client_metadata"


def test_non_https_non_loopback_redirect_rejected():
    resp = _client(_ENABLED).post(
        "/oauth/register",
        json={"redirect_uris": ["http://evil.example.com/cb"]},
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_redirect_uri"


def test_https_and_loopback_http_accepted():
    resp = _client(_ENABLED).post(
        "/oauth/register",
        json={
            "redirect_uris": [
                "https://a.example.com/cb",
                "http://127.0.0.1/cb",
            ]
        },
    )
    assert resp.status_code == 201


def test_too_many_redirect_uris_returns_400():
    resp = _client(_ENABLED).post(
        "/oauth/register",
        json={
            "redirect_uris": [
                f"https://a{i}.example.com/cb" for i in range(6)
            ]
        },
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_client_metadata"


def test_confidential_auth_method_rejected():
    resp = _client(_ENABLED).post(
        "/oauth/register",
        json={
            "redirect_uris": ["https://app.example.com/cb"],
            "token_endpoint_auth_method": "client_secret_post",
        },
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_client_metadata"


def test_unsupported_scope_is_narrowed():
    resp = _client(_ENABLED).post(
        "/oauth/register",
        json={
            "redirect_uris": ["https://app.example.com/cb"],
            "scope": "menhir:read foo:bar",
        },
    )
    assert resp.status_code == 201
    assert resp.json()["scope"] == "menhir:read"


def test_two_registrations_get_distinct_client_ids():
    client = _client(_ENABLED)
    body = {"redirect_uris": ["https://app.example.com/cb"]}
    resp1 = client.post("/oauth/register", json=body)
    resp2 = client.post("/oauth/register", json=body)
    assert resp1.status_code == 201
    assert resp2.status_code == 201
    assert resp1.json()["client_id"] != resp2.json()["client_id"]
