"""Router-wide response scrub (#577)."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.testclient import TestClient

from menhir.api import routes as api_routes
from menhir.api.scrub_route import ScrubbingAPIRoute

pytestmark = pytest.mark.unit

AWS = "AKIAIOSFODNN7EXAMPLE"
GH = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"


@pytest.fixture(autouse=True)
def _policy(monkeypatch):
    monkeypatch.delenv("MENHIR_SECRET_POLICY", raising=False)


def _toy_client() -> TestClient:
    r = APIRouter(route_class=ScrubbingAPIRoute)

    @r.get("/aws")
    async def aws():
        return {"items": [{"content": f"key {AWS}", "n": 1}]}

    @r.get("/gh")
    async def gh():
        return {"items": [{"content": f"tok {GH}", "n": 2, "flag": True}]}

    @r.get("/boom")
    async def boom():
        raise HTTPException(status_code=418, detail="teapot")

    app = FastAPI()
    app.include_router(r)
    return TestClient(app)


def test_aws_key_redacted():
    resp = _toy_client().get("/aws")
    assert resp.status_code == 200
    assert AWS not in resp.text
    assert "[REDACTED:" in resp.text
    assert resp.json()["items"][0]["n"] == 1


def test_github_token_redacted_and_non_strings_kept():
    resp = _toy_client().get("/gh")
    assert GH not in resp.text
    assert "[REDACTED:" in resp.text
    item = resp.json()["items"][0]
    assert item["n"] == 2 and item["flag"] is True


def test_policy_off_returns_body_unchanged(monkeypatch):
    monkeypatch.setenv("MENHIR_SECRET_POLICY", "off")
    resp = _toy_client().get("/aws")
    assert AWS in resp.text


def test_detector_failure_withholds(monkeypatch):
    def _raise(*_a, **_k):
        raise RuntimeError("detector down")

    monkeypatch.setattr("menhir.domain.payload_scrub.redact", _raise)
    resp = _toy_client().get("/gh")
    assert resp.status_code == 500
    assert resp.json() == {"detail": "Output withheld: the credential scan failed."}
    assert GH not in resp.text


def test_http_exception_propagates():
    resp = _toy_client().get("/boom")
    assert resp.status_code == 418
    assert resp.json()["detail"] == "teapot"


def test_main_router_uses_scrubbing_route():
    assert api_routes.router.route_class is ScrubbingAPIRoute


def _real_client(backend=None, adapter=None) -> TestClient:
    app = FastAPI()
    app.include_router(api_routes.router)
    app.state.runtime_ctx = SimpleNamespace(
        built=SimpleNamespace(graph_adapter=adapter), capabilities=None
    )
    return app, backend


def test_real_bootstrap_flagged_scrubbed():
    backend = SimpleNamespace(
        fetch_flagged_memories=AsyncMock(
            return_value=[{"uuid": "u1", "name": "pin", "content": f"secret {GH}", "n": 3}]
        ),
        fetch_flagged_memory_bootstrap_version=AsyncMock(return_value="v1"),
    )
    app, _ = _real_client(backend)
    with patch.object(api_routes, "_get_backend", return_value=backend):
        resp = TestClient(app).get("/api/bootstrap/flagged")
    assert resp.status_code == 200, resp.text
    assert GH not in resp.text
    assert "[REDACTED:" in resp.text
    assert resp.json()["items"][0]["n"] == 3


def test_real_scalar_view_items_scrubbed():
    adapter = SimpleNamespace(
        fetch_scalar_authority_contributors=lambda **kw: {
            "total": 1,
            "next_offset": None,
            "contributors": [
                {"assertion_id": "a1", "stated_span": f"my key is {AWS}", "value": 5}
            ],
        },
        list_scalar_history_entries=lambda **kw: {},
    )
    app, _ = _real_client(adapter=adapter)
    resp = TestClient(app).get("/api/scalar-views/v1/items")
    assert resp.status_code == 200, resp.text
    assert AWS not in resp.text
    assert "[REDACTED:" in resp.text
    body = resp.json()
    assert body["total"] == 1 and body["items"][0]["value"] == 5
