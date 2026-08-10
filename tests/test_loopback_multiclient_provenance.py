"""Tests for loopback no-auth per-client provenance capture.

In loopback no-auth mode (no static keys, OAuth disabled), self-declared
x-yawn-client-name headers and ?client_name= query params are captured for
telemetry/provenance without changing access/tier behavior.
"""

from __future__ import annotations

import hashlib

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from menhir.api.auth import BearerAuthMiddleware
from menhir.mcp.service_access import get_request_session


def _build_app() -> TestClient:
    """Build an app with no auth keys (loopback no-auth mode)."""
    app = FastAPI()

    @app.get("/api/secure")
    async def secure():
        session = get_request_session()
        return JSONResponse(
            {
                "secure": True,
                "user_id": session.user_id if session else "",
                "session_id": session.session_id if session else "",
                "client_id": session.client_id if session else "",
                "client_name": session.client_name if session else "",
            }
        )

    @app.get("/api/health")
    async def health():
        return JSONResponse({"ok": True})

    @app.get("/mcp")
    async def mcp_root(client_name: str | None = None):
        session = get_request_session()
        return JSONResponse(
            {
                "client_name_query": client_name,
                "client_id": session.client_id if session else "",
                "client_name": session.client_name if session else "",
            }
        )

    @app.get("/mcp-http")
    async def mcp_http(client_name: str | None = None):
        session = get_request_session()
        return JSONResponse(
            {
                "client_name_query": client_name,
                "client_id": session.client_id if session else "",
                "client_name": session.client_name if session else "",
            }
        )

    wrapped = BearerAuthMiddleware(app)
    return TestClient(wrapped)


class TestLoopbackMulticlientProvenance:
    """Per-client provenance in loopback no-auth mode."""

    def test_two_named_clients_get_distinct_identity(self):
        """Two no-auth requests with different x-yawn-client-name bind distinct client_ids."""
        client = _build_app()

        resp_a = client.get(
            "/api/secure",
            headers={"x-yawn-client-name": "alpha"},
        )
        assert resp_a.status_code == 200
        data_a = resp_a.json()
        assert data_a["client_name"] == "alpha"

        resp_b = client.get(
            "/api/secure",
            headers={"x-yawn-client-name": "beta"},
        )
        assert resp_b.status_code == 200
        data_b = resp_b.json()
        assert data_b["client_name"] == "beta"

        # client_id should be derived from the name (stable hash)
        assert data_a["client_id"] != data_b["client_id"]
        assert len(data_a["client_id"]) == 16
        assert len(data_b["client_id"]) == 16

        # Verify the derived client_id is a sha256 hash of the name
        expected_a = hashlib.sha256(b"alpha").hexdigest()[:16]
        expected_b = hashlib.sha256(b"beta").hexdigest()[:16]
        assert data_a["client_id"] == expected_a
        assert data_b["client_id"] == expected_b

    def test_client_name_via_query_on_mcp_path(self):
        """MCP path with ?client_name=gamma binds gamma as client_name."""
        client = _build_app()

        resp = client.get("/mcp-http?client_name=gamma")
        assert resp.status_code == 200
        data = resp.json()
        assert data["client_name_query"] == "gamma"
        assert data["client_name"] == "gamma"
        # client_id derived from "gamma"
        expected_id = hashlib.sha256(b"gamma").hexdigest()[:16]
        assert data["client_id"] == expected_id

    def test_client_id_stable_across_calls(self):
        """Same client_name on two calls yields the same derived client_id."""
        client = _build_app()

        resp1 = client.get(
            "/api/secure",
            headers={"x-yawn-client-name": "claude-code"},
        )
        assert resp1.status_code == 200
        data1 = resp1.json()

        resp2 = client.get(
            "/api/secure",
            headers={"x-yawn-client-name": "claude-code"},
        )
        assert resp2.status_code == 200
        data2 = resp2.json()

        assert data1["client_id"] == data2["client_id"]
        assert data1["client_name"] == data2["client_name"] == "claude-code"

    def test_unlabeled_client_uses_default_bucket(self):
        """A no-auth request with no client label uses the default identity."""
        client = _build_app()

        resp = client.get("/api/secure")
        assert resp.status_code == 200
        data = resp.json()

        # Defaults: user_id -> "remote-api", client_name -> "remote-api"
        assert data["user_id"] == "remote-api"
        assert data["client_name"] == "remote-api"
        # No client_name -> no hash -> empty client_id
        assert data["client_id"] == ""

    def test_no_auth_access_unchanged(self):
        """No-key loopback request to a protected route still succeeds (no tier regression)."""
        client = _build_app()

        resp = client.get("/api/secure")
        assert resp.status_code == 200
        assert resp.json()["secure"] is True

        resp = client.get("/mcp")
        assert resp.status_code == 200

        resp = client.get("/mcp-http")
        assert resp.status_code == 200

    def test_health_is_exempt_in_no_auth_mode(self):
        """Health endpoint is exempt in no-auth mode (same as with keys)."""
        client = _build_app()
        resp = client.get("/api/health")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_name_fallback_excluded_on_oauth_path(self):
        """The client_name->client_id fallback must NOT fire when identity is
        not self-declared (OAuth path: trust_identity_headers=False). An empty
        principal client_id must stay empty rather than be derived from a name."""
        headers = {b"x-yawn-client-name": b"should-be-ignored"}
        _user, _session, client_id, _name = BearerAuthMiddleware._request_session_headers(
            headers,
            path="/mcp-http",
            api_key="",
            default_client_name="oauth-client",
            trust_identity_headers=False,
        )
        # No name-derived id on the OAuth path.
        assert client_id == ""
