"""CF-27: the client-token auth path offloads its blocking sqlite read off the event loop.

`ClientTokenStore.resolve()` opens a blocking ``sqlite3`` connection per call. Before this
change ``_call_with_client_token`` ran it synchronously on the event loop, serializing every
concurrent authenticated request. The fix wraps both call sites in ``asyncio.to_thread``,
matching the local convention in ``routes.py``.

These tests prove (a) the offload actually happens, (b) the auth outcome is byte-for-byte
unchanged for valid / unknown / revoked tokens, and (c) the ``if token_str`` short-circuit is
preserved so a falsy token never triggers a ``resolve``.
"""

from __future__ import annotations

import asyncio

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from menhir.api.auth import BearerAuthMiddleware
from menhir.api.client_token_store import ClientTokenStore
from menhir.mcp.service_access import get_request_session, get_request_tier


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

    return TestClient(BearerAuthMiddleware(app, client_token_store=store))


class TestOffloadedToThread:
    """The blocking resolve is actually moved off the event loop."""

    async def test_resolve_is_called_through_asyncio_to_thread(self, tmp_path, monkeypatch):
        store = ClientTokenStore(tmp_path / "ct.db")
        raw, _ = store.mint("alpha", "operator")
        client = _build(store)

        calls: list[tuple] = []
        real_to_thread = asyncio.to_thread

        async def recording_to_thread(fn, *args, **kwargs):
            calls.append((fn, args, kwargs))
            return await real_to_thread(fn, *args, **kwargs)

        monkeypatch.setattr("menhir.api.auth.asyncio.to_thread", recording_to_thread)

        resp = client.get("/api/secure", headers={"Authorization": f"Bearer {raw}"})
        assert resp.status_code == 200

        assert calls, "asyncio.to_thread was never called on the client-token auth path"
        assert any(fn == store.resolve for fn, _, _ in calls), (
            "to_thread must be called with ClientTokenStore.resolve"
        )


class TestAuthOutcomeUnchanged:
    """The auth decision must be identical to before the offload."""

    def test_valid_token_authenticates_and_binds_identity_tier(self, tmp_path):
        store = ClientTokenStore(tmp_path / "ct.db")
        raw, record = store.mint("alpha", "operator")
        client = _build(store)

        resp = client.get("/api/secure", headers={"Authorization": f"Bearer {raw}"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["client_id"] == record.client_id
        assert data["client_name"] == "alpha"
        assert data["tier"] == "operator"

    def test_unknown_token_rejected(self, tmp_path):
        store = ClientTokenStore(tmp_path / "ct.db")
        store.mint("alpha", "operator")
        client = _build(store)

        resp = client.get("/api/secure", headers={"Authorization": "Bearer not-a-real-token"})
        assert resp.status_code == 401

    def test_revoked_token_rejected(self, tmp_path):
        store = ClientTokenStore(tmp_path / "ct.db")
        raw, record = store.mint("alpha", "operator")
        assert store.revoke(record.client_id) is True
        client = _build(store)

        resp = client.get("/api/secure", headers={"Authorization": f"Bearer {raw}"})
        assert resp.status_code == 401


class TestShortCircuitPreserved:
    """A falsy/absent token must NOT trigger a resolve at all."""

    async def test_resolve_not_called_without_token(self, tmp_path, monkeypatch):
        store = ClientTokenStore(tmp_path / "ct.db")
        client = _build(store)

        calls: list[tuple] = []

        def explode_resolve(token):
            calls.append(token)
            raise AssertionError("resolve must not be called when token is falsy")

        monkeypatch.setattr(store, "resolve", explode_resolve)
        # Also guard the to_thread hop so a stray call cannot be swallowed.
        monkeypatch.setattr(
            "menhir.api.auth.asyncio.to_thread",
            lambda *a, **k: (_ for _ in ()).throw(
                AssertionError("to_thread must not be called when token is falsy")
            ),
        )

        resp = client.get("/api/secure")
        assert resp.status_code == 401
        assert calls == [], "resolve was invoked despite a falsy token"
