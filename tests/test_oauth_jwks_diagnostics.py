"""JWKS fetch failures must be diagnosable from the server log and /readyz.

A production 503 "Unable to fetch OAuth JWKS" (2026-09-22) left no record of
why the fetch failed and no request id in the log. These tests pin the
diagnostics without changing any auth outcome.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from menhir.api import oauth as oauth_module
from menhir.api.auth import BearerAuthMiddleware
from menhir.api.oauth import (
    JWKS_REFRESH_STATS,
    OAuthAuthenticationError,
    OAuthConfig,
    OAuthTokenVerifier,
)
from menhir.api.production_routes import readyz
from menhir.api.request_context import RequestContextMiddleware

_SECRET_URI = "https://user:hunter2@auth.example.com/.well-known/jwks.json?sig=topsecret"


def _config(jwks_uri: str = _SECRET_URI) -> OAuthConfig:
    return OAuthConfig(
        enabled=True,
        public_base_url="https://memory.example.com",
        resource="https://memory.example.com/mcp-http",
        authorization_servers=("https://auth.example.com",),
        issuer="https://auth.example.com/",
        jwks_uri=jwks_uri,
        audiences=("https://memory.example.com/mcp-http",),
    )


@pytest.fixture(autouse=True)
def _reset_stats():
    JWKS_REFRESH_STATS.failures = 0
    JWKS_REFRESH_STATS.last_failure_at = None
    JWKS_REFRESH_STATS.last_failure_kind = None
    yield


def _patch_client(monkeypatch, *, raise_exc: Exception | None = None, payload=None):
    class _Resp:
        def raise_for_status(self) -> None:
            return None

        def json(self):
            return payload

    class _Client:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc) -> None:
            return None

        async def get(self, url):
            if raise_exc is not None:
                raise raise_exc
            return _Resp()

    monkeypatch.setattr(oauth_module.httpx, "AsyncClient", _Client)


@pytest.mark.asyncio
async def test_fetch_failure_logs_kind_and_redacted_uri(monkeypatch, caplog):
    _patch_client(
        monkeypatch,
        raise_exc=httpx.ConnectTimeout(f"timed out connecting to {_SECRET_URI}"),
    )
    verifier = OAuthTokenVerifier(_config())

    with caplog.at_level(logging.WARNING, logger="menhir.api.oauth"):
        with pytest.raises(OAuthAuthenticationError) as exc:
            await verifier._load_jwks()

    # Auth outcome unchanged.
    assert exc.value.error == "server_error"
    assert exc.value.description == "Unable to fetch OAuth JWKS"
    text = caplog.text
    assert "OAuth JWKS fetch failed" in text
    assert "kind=ConnectTimeout" in text
    assert "https://auth.example.com/.well-known/jwks.json" in text
    assert "hunter2" not in text and "topsecret" not in text
    assert JWKS_REFRESH_STATS.failures == 1
    assert JWKS_REFRESH_STATS.last_failure_kind == "ConnectTimeout"
    assert JWKS_REFRESH_STATS.last_failure_at is not None


@pytest.mark.asyncio
async def test_malformed_payload_is_logged_and_counted(monkeypatch, caplog):
    _patch_client(monkeypatch, payload={"not": "a key set"})
    verifier = OAuthTokenVerifier(_config())

    with caplog.at_level(logging.WARNING, logger="menhir.api.oauth"):
        with pytest.raises(OAuthAuthenticationError) as exc:
            await verifier._load_jwks()

    assert exc.value.description == "OAuth JWKS response is malformed"
    assert "kind=MalformedJWKS" in caplog.text
    assert JWKS_REFRESH_STATS.failures == 1


class _FailingVerifier:
    async def verify_access_token(self, token: str):
        try:
            raise httpx.ReadTimeout("read timed out")
        except httpx.ReadTimeout as cause:
            raise OAuthAuthenticationError(
                "server_error", "Unable to fetch OAuth JWKS"
            ) from cause


def test_503_log_line_carries_the_request_id_the_client_sees(caplog):
    app = FastAPI()

    @app.get("/mcp/test")
    async def mcp_test():
        return JSONResponse({"ok": True})

    wrapped = RequestContextMiddleware(
        BearerAuthMiddleware(
            app, api_key="", oauth_config=_config(), oauth_verifier=_FailingVerifier()
        )
    )
    client = TestClient(wrapped)

    with caplog.at_level(logging.WARNING, logger="menhir.api.auth"):
        resp = client.get("/mcp/test", headers={"Authorization": "Bearer tok"})

    assert resp.status_code == 503
    request_id = resp.json()["request_id"]
    assert request_id
    assert f"request_id={request_id}" in caplog.text
    assert "cause=ReadTimeout" in caplog.text
    assert "path=/mcp/test" in caplog.text
    assert "Bearer tok" not in caplog.text


def _ready_state() -> SimpleNamespace:
    return SimpleNamespace(
        settings=SimpleNamespace(runtime_mode="production", instance_id="x"),
        runtime_ctx=SimpleNamespace(
            capabilities=SimpleNamespace(neo4j_ready=True, enrichment_ready=True),
            scheduler=object(),
        ),
        oauth_signing_key=object(),
    )


@pytest.mark.asyncio
async def test_readyz_reports_jwks_failures_without_changing_readiness():
    import json

    JWKS_REFRESH_STATS.record("ConnectTimeout")
    response = await readyz(SimpleNamespace(app=SimpleNamespace(state=_ready_state())))

    assert response.status_code == 200
    body = json.loads(response.body)
    assert body["status"] == "ready"
    assert body["failures"] == []
    assert body["oauth_jwks"]["refresh_failures"] == 1
    assert body["oauth_jwks"]["last_failure_kind"] == "ConnectTimeout"
    assert body["oauth_jwks"]["last_failure_at"]
