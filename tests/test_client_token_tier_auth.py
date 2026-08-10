"""Tests for the enforced per-client token tier in BearerAuthMiddleware.

When a ClientTokenStore is supplied, the middleware owns protected auth: each
bearer token resolves to a *registered* identity/tier, self-declared headers are
ignored (tamper-proof), and unknown/revoked tokens are rejected 401.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from menhir.api.auth import BearerAuthMiddleware
from menhir.api.client_token_store import ClientTokenStore
from menhir.api.routes import router as api_router
from menhir.mcp.service_access import get_request_session, get_request_tier


async def _ok_app(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


def _http_scope(path, client, *, headers=None, method="POST"):
    return {
        "type": "http",
        "http_version": "1.1",
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
        "client": client,
        "server": ("127.0.0.1", 8100),
        "scheme": "http",
    }


async def _drive(mw, scope) -> int:
    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    sent: list[dict] = []

    async def send(msg):
        sent.append(msg)

    await mw(scope, receive, send)
    return next(m["status"] for m in sent if m["type"] == "http.response.start")


def _admin_app(store: ClientTokenStore) -> FastAPI:
    app = FastAPI()
    app.state.client_token_store = store

    @app.get("/api/whoami")
    async def whoami():
        session = get_request_session()
        return JSONResponse(
            {"client_name": session.client_name if session else "", "tier": get_request_tier()}
        )

    app.include_router(api_router)
    return app


def _build(store: ClientTokenStore) -> TestClient:
    app = FastAPI()

    @app.get("/api/secure")
    async def secure():
        session = get_request_session()
        return JSONResponse(
            {
                "client_id": session.client_id if session else "",
                "client_name": session.client_name if session else "",
                "tier": get_request_tier(),
            }
        )

    @app.get("/mcp-http")
    async def mcp_http():
        session = get_request_session()
        return JSONResponse(
            {
                "client_id": session.client_id if session else "",
                "client_name": session.client_name if session else "",
                "tier": get_request_tier(),
            }
        )

    return TestClient(BearerAuthMiddleware(app, client_token_store=store))


class TestClientTokenTier:
    def test_valid_token_binds_registered_identity_and_tier(self, tmp_path):
        store = ClientTokenStore(tmp_path / "ct.db")
        raw, record = store.mint("alpha", "operator")
        client = _build(store)

        resp = client.get("/api/secure", headers={"Authorization": f"Bearer {raw}"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["client_id"] == record.client_id
        assert data["client_name"] == "alpha"
        assert data["tier"] == "operator"

    def test_header_cannot_override_registered_identity(self, tmp_path):
        """Tamper-proof: a self-declared x-yawn-client-name is ignored."""
        store = ClientTokenStore(tmp_path / "ct.db")
        raw, _ = store.mint("alpha", "operator")
        client = _build(store)

        resp = client.get(
            "/api/secure",
            headers={
                "Authorization": f"Bearer {raw}",
                "x-yawn-client-name": "evil",
                "x-yawn-client-id": "spoofed",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["client_name"] == "alpha"  # not "evil"
        assert data["client_id"] != "spoofed"

    def test_unknown_token_rejected(self, tmp_path):
        store = ClientTokenStore(tmp_path / "ct.db")
        store.mint("alpha", "operator")
        client = _build(store)

        resp = client.get("/api/secure", headers={"Authorization": "Bearer not-a-real-token"})
        assert resp.status_code == 401

    def test_missing_token_rejected(self, tmp_path):
        store = ClientTokenStore(tmp_path / "ct.db")
        client = _build(store)

        resp = client.get("/api/secure")
        assert resp.status_code == 401

    def test_revoked_token_rejected(self, tmp_path):
        store = ClientTokenStore(tmp_path / "ct.db")
        raw, record = store.mint("alpha", "operator")
        assert store.revoke(record.client_id) is True
        client = _build(store)

        resp = client.get("/api/secure", headers={"Authorization": f"Bearer {raw}"})
        assert resp.status_code == 401

    def test_query_token_on_mcp_path(self, tmp_path):
        store = ClientTokenStore(tmp_path / "ct.db")
        raw, record = store.mint("gamma", "readonly")
        client = _build(store)

        resp = client.get(f"/mcp-http?api_key={raw}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["client_name"] == "gamma"
        assert data["tier"] == "readonly"

    def test_two_clients_bind_distinct_identity(self, tmp_path):
        store = ClientTokenStore(tmp_path / "ct.db")
        raw_a, rec_a = store.mint("alpha", "agent")
        raw_b, rec_b = store.mint("beta", "readonly")
        client = _build(store)

        a = client.get("/api/secure", headers={"Authorization": f"Bearer {raw_a}"}).json()
        b = client.get("/api/secure", headers={"Authorization": f"Bearer {raw_b}"}).json()
        assert a["client_id"] == rec_a.client_id
        assert b["client_id"] == rec_b.client_id
        assert a["client_id"] != b["client_id"]
        assert a["tier"] == "agent"
        assert b["tier"] == "readonly"


class TestAdminGate:
    """Admin gate: loopback-no-token may ONLY mint (bootstrap); revoke and other
    admin actions require the operator key or an operator-tier token (provenance)."""

    async def test_loopback_can_mint_when_store_empty(self, tmp_path):
        store = ClientTokenStore(tmp_path / "ct.db")  # empty -> bootstrap open
        mw = BearerAuthMiddleware(_ok_app, client_token_store=store, loopback_bound=True)
        status = await _drive(mw, _http_scope("/api/admin/clients", ("127.0.0.1", 5555)))
        assert status == 200

    async def test_bootstrap_denied_with_forwarding_header(self, tmp_path):
        """CT-001: a loopback-peer bootstrap POST carrying any reverse-proxy
        forwarding header is refused. Behind a same-host proxy every external
        request presents peer 127.0.0.1, so a forwarding header is the tell that
        the caller is not truly local."""
        for header in ("x-forwarded-for", "x-real-ip", "forwarded"):
            store = ClientTokenStore(tmp_path / f"ct-{header}.db")  # empty -> bootstrap open
            mw = BearerAuthMiddleware(_ok_app, client_token_store=store, loopback_bound=True)
            status = await _drive(
                mw,
                _http_scope(
                    "/api/admin/clients",
                    ("127.0.0.1", 5555),
                    headers={header: "203.0.113.9"},
                ),
            )
            assert status == 401, f"forwarding header {header!r} must block bootstrap"

    async def test_loopback_get_list_is_not_bootstrap(self, tmp_path):
        """Bootstrap is POST-mint only: a GET to the admin path from loopback
        (even with an empty store) is not authorized without a credential."""
        store = ClientTokenStore(tmp_path / "ct.db")
        mw = BearerAuthMiddleware(_ok_app, client_token_store=store, loopback_bound=True)
        status = await _drive(
            mw, _http_scope("/api/admin/clients", ("127.0.0.1", 5555), method="GET")
        )
        assert status == 401

    async def test_loopback_mint_blocked_once_active_token_exists(self, tmp_path):
        """TOFU: after the first token exists, loopback-no-token can no longer mint."""
        store = ClientTokenStore(tmp_path / "ct.db")
        store.mint("bootstrap-admin", "operator")  # store now has an active token
        mw = BearerAuthMiddleware(_ok_app, client_token_store=store, loopback_bound=True)
        status = await _drive(mw, _http_scope("/api/admin/clients", ("127.0.0.1", 5555)))
        assert status == 401

    async def test_loopback_bootstrap_reopens_after_all_revoked(self, tmp_path):
        """Revoking the last active token re-opens loopback bootstrap (reset path)."""
        store = ClientTokenStore(tmp_path / "ct.db")
        _raw, rec = store.mint("bootstrap-admin", "operator")
        store.revoke(rec.client_id)  # 0 active tokens again
        mw = BearerAuthMiddleware(_ok_app, client_token_store=store, loopback_bound=True)
        status = await _drive(mw, _http_scope("/api/admin/clients", ("127.0.0.1", 5555)))
        assert status == 200

    async def test_loopback_cannot_revoke_without_token(self, tmp_path):
        """Bootstrap is mint-only: loopback-no-token may NOT revoke (no provenance)."""
        store = ClientTokenStore(tmp_path / "ct.db")
        _raw, rec = store.mint("victim", "agent")
        mw = BearerAuthMiddleware(_ok_app, client_token_store=store, loopback_bound=True)
        status = await _drive(
            mw, _http_scope(f"/api/admin/clients/{rec.client_id}/revoke", ("127.0.0.1", 5555))
        )
        assert status == 401

    async def test_operator_tier_token_can_revoke(self, tmp_path):
        """An operator-tier minted token carries provenance and may revoke."""
        store = ClientTokenStore(tmp_path / "ct.db")
        op_raw, _ = store.mint("admin-client", "operator")
        _raw, victim = store.mint("victim", "agent")
        mw = BearerAuthMiddleware(_ok_app, client_token_store=store, loopback_bound=True)
        status = await _drive(
            mw,
            _http_scope(
                f"/api/admin/clients/{victim.client_id}/revoke",
                ("203.0.113.9", 5555),
                headers={"authorization": f"Bearer {op_raw}"},
            ),
        )
        assert status == 200

    async def test_non_operator_tier_token_cannot_admin(self, tmp_path):
        """An agent/readonly minted token must NOT reach admin endpoints."""
        store = ClientTokenStore(tmp_path / "ct.db")
        agent_raw, _ = store.mint("agent-client", "agent")
        mw = BearerAuthMiddleware(_ok_app, client_token_store=store, loopback_bound=True)
        status = await _drive(
            mw,
            _http_scope(
                "/api/admin/clients",
                ("203.0.113.9", 5555),
                headers={"authorization": f"Bearer {agent_raw}"},
            ),
        )
        assert status == 401

    async def test_loopback_origin_rejected_when_not_loopback_bound(self, tmp_path):
        """On a network bind, a 127.0.0.1-appearing request (reverse proxy) must NOT get admin."""
        store = ClientTokenStore(tmp_path / "ct.db")
        mw = BearerAuthMiddleware(_ok_app, client_token_store=store, loopback_bound=False)
        status = await _drive(mw, _http_scope("/api/admin/clients", ("127.0.0.1", 5555)))
        assert status == 401

    async def test_remote_origin_rejected_without_operator_key(self, tmp_path):
        store = ClientTokenStore(tmp_path / "ct.db")
        mw = BearerAuthMiddleware(_ok_app, client_token_store=store, loopback_bound=True)
        status = await _drive(mw, _http_scope("/api/admin/clients", ("203.0.113.9", 5555)))
        assert status == 401

    async def test_operator_key_allows_remote_admin(self, tmp_path):
        store = ClientTokenStore(tmp_path / "ct.db")
        mw = BearerAuthMiddleware(
            _ok_app, operator_key="op-secret", client_token_store=store, loopback_bound=False
        )
        status = await _drive(
            mw,
            _http_scope(
                "/api/admin/clients",
                ("203.0.113.9", 5555),
                headers={"authorization": "Bearer op-secret"},
            ),
        )
        assert status == 200

    async def test_wrong_operator_key_remote_rejected(self, tmp_path):
        store = ClientTokenStore(tmp_path / "ct.db")
        mw = BearerAuthMiddleware(
            _ok_app, operator_key="op-secret", client_token_store=store, loopback_bound=False
        )
        status = await _drive(
            mw,
            _http_scope(
                "/api/admin/clients",
                ("203.0.113.9", 5555),
                headers={"authorization": "Bearer wrong"},
            ),
        )
        assert status == 401


class TestMintRevokeEndToEnd:
    """Full flow through the real router: loopback admin mints, client uses, admin revokes."""

    # NOTE: Starlette's TestClient hard-codes scope["client"] to
    # ("testclient", 50000), so the loopback-origin admin path cannot be
    # exercised through TestClient (that path is covered by TestAdminGate via
    # raw ASGI). These end-to-end tests use the operator-key admin path instead.
    _OP = "op-secret"
    _ADMIN_HDR = {"Authorization": f"Bearer {_OP}"}

    def _client(self, store):
        app = _admin_app(store)
        return TestClient(
            BearerAuthMiddleware(
                app, operator_key=self._OP, client_token_store=store, loopback_bound=False
            )
        )

    def test_mint_use_revoke_flow(self, tmp_path):
        store = ClientTokenStore(tmp_path / "ct.db")
        client = self._client(store)

        # 1. mint (operator-key admin)
        resp = client.post(
            "/api/admin/clients",
            json={"client_name": "claude-code", "tier": "agent"},
            headers=self._ADMIN_HDR,
        )
        assert resp.status_code == 200, resp.text
        minted = resp.json()
        raw = minted["token"]
        cid = minted["client_id"]
        assert minted["tier"] == "agent"

        # 2. use the minted token on a protected route -> registered identity + tier
        who = client.get("/api/whoami", headers={"Authorization": f"Bearer {raw}"})
        assert who.status_code == 200
        assert who.json() == {"client_name": "claude-code", "tier": "agent"}

        # 3. revoke, then the token is rejected
        rev = client.post(f"/api/admin/clients/{cid}/revoke", headers=self._ADMIN_HDR)
        assert rev.status_code == 200
        assert rev.json()["revoked"] is True

        who2 = client.get("/api/whoami", headers={"Authorization": f"Bearer {raw}"})
        assert who2.status_code == 401

    def test_mint_rejects_invalid_tier(self, tmp_path):
        store = ClientTokenStore(tmp_path / "ct.db")
        client = self._client(store)
        resp = client.post(
            "/api/admin/clients",
            json={"client_name": "x", "tier": "superuser"},
            headers=self._ADMIN_HDR,
        )
        assert resp.status_code == 422

    def test_list_clients_returns_summaries_without_tokens(self, tmp_path):
        store = ClientTokenStore(tmp_path / "ct.db")
        client = self._client(store)
        client.post(
            "/api/admin/clients", json={"client_name": "a", "tier": "agent"}, headers=self._ADMIN_HDR
        )
        client.post(
            "/api/admin/clients", json={"client_name": "b", "tier": "readonly"}, headers=self._ADMIN_HDR
        )
        resp = client.get("/api/admin/clients", headers=self._ADMIN_HDR)
        assert resp.status_code == 200
        clients = resp.json()["clients"]
        assert {c["client_name"] for c in clients} == {"a", "b"}
        # No token material is ever exposed in the listing.
        assert all("token" not in c for c in clients)

    def test_list_clients_requires_credential(self, tmp_path):
        """GET list is not a bootstrap path: no credential -> 401 even on loopback-bound."""
        store = ClientTokenStore(tmp_path / "ct.db")
        # loopback_bound but no operator header, TestClient origin is non-loopback anyway
        app = _admin_app(store)
        client = TestClient(BearerAuthMiddleware(app, client_token_store=store, loopback_bound=True))
        resp = client.get("/api/admin/clients")
        assert resp.status_code == 401

    def test_minted_token_cannot_reach_admin(self, tmp_path):
        """A normal minted client token must NOT be able to mint/revoke."""
        store = ClientTokenStore(tmp_path / "ct.db")
        client = self._client(store)
        raw, _ = store.mint("agent-client", "agent")
        # Present the client token (not the operator key) to an admin route.
        resp = client.post(
            "/api/admin/clients",
            json={"client_name": "y", "tier": "readonly"},
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert resp.status_code == 401
