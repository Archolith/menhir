"""Contract tests for /api/ready and /api/stats endpoints.

No server startup, no Neo4j, no Docker, no model calls.
Tests use FastAPI TestClient with mocked runtime context and backend.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from menhir.api import routes as api_routes
from menhir.api.auth import BearerAuthMiddleware
from menhir.api.routes import router
from menhir.config import MemorySettings


# ===================================================================
# Helpers
# ===================================================================

def _fake_runtime_ctx(**overrides):
    capabilities_kw = {
        "startup_mode": "full",
        "neo4j_ready": True,
        "embedder_ready": True,
        "llm_ready": True,
        "scheduler_ready": True,
        "reads_ready": True,
        "queue_writes_ready": True,
        "enrichment_ready": True,
        "graphiti_ready": True,
        "failures": [],
    }
    capabilities_kw.update(overrides.get("capabilities", {}))
    capabilities = SimpleNamespace(**capabilities_kw)

    built = SimpleNamespace(
        ingest_service=SimpleNamespace(
            enrichment_enabled=lambda: True,
            wait_for_episode_processing=AsyncMock(),
        ),
        scheduler=SimpleNamespace(status_snapshot=lambda: {"running": True}),
        settings=SimpleNamespace(personal_memory_consolidation_chat_model="pm-model"),
    )
    return SimpleNamespace(
        built=built,
        session=SimpleNamespace(session_id="process-session", user_id="claude-code"),
        capabilities=capabilities,
    )


def _fake_backend(**overrides):
    backend = SimpleNamespace()
    backend.fetch_operation_stats = AsyncMock(
        return_value=overrides.get("operation_stats", [])
    )
    backend.fetch_enrichment_rate = AsyncMock(
        return_value=overrides.get("enrichment_rate", {"total": 10, "successes": 8})
    )
    backend.get_queue_depth = AsyncMock(
        return_value=overrides.get("queue_depth", 5)
    )
    backend.scheduler_status_snapshot = AsyncMock(
        return_value=overrides.get("scheduler_snapshot", {"running": True})
    )
    return backend


def _check_no_secrets(d: dict) -> None:
    """Assert no raw secret values appear in the JSON output."""
    dumped = json.dumps(d)
    for secret in (
        "super-secret-agent-key",
        "super-secret-readonly-key",
        "super-secret-admin-key",
        "neo4j-super-secret-password",
        "openai-super-secret-key",
    ):
        assert secret not in dumped, f"secret leaked: {secret}"


# ===================================================================
# TestReady — GET /api/ready
# ===================================================================

class TestReady:
    """Contract tests for GET /api/ready."""

    @pytest.mark.unit
    def test_ready_ok(self):
        """Returns status='ready' when enrichment is ready."""
        app = FastAPI()
        app.include_router(router)
        app.state.runtime_ctx = _fake_runtime_ctx()
        client = TestClient(app)

        resp = client.get("/api/ready")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ready"
        assert data["startup_mode"] == "full"
        assert data["capabilities"]["enrichment_ready"] is True
        assert data["capabilities"]["neo4j_ready"] is True
        assert data["failures"] == []

    @pytest.mark.unit
    def test_ready_starting_when_no_runtime(self):
        """Returns status='starting' when runtime is not initialized."""
        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)

        resp = client.get("/api/ready")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "starting"
        assert data["startup_mode"] == "unavailable"
        assert data["capabilities"]["enrichment_ready"] is False
        assert data["failures"] == ["runtime not initialized"]

    @pytest.mark.unit
    def test_ready_degraded_when_enrichment_not_ready(self):
        """Returns status='degraded' when enrichment is not ready."""
        app = FastAPI()
        app.include_router(router)
        ctx = _fake_runtime_ctx(capabilities={"enrichment_ready": False})
        app.state.runtime_ctx = ctx
        client = TestClient(app)

        resp = client.get("/api/ready")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "degraded"

    @pytest.mark.unit
    def test_ready_propagates_failures(self):
        """Failures list is propagated from capabilities."""
        app = FastAPI()
        app.include_router(router)
        ctx = _fake_runtime_ctx(capabilities={
            "enrichment_ready": False,
            "failures": ["neo4j unavailable"],
        })
        app.state.runtime_ctx = ctx
        client = TestClient(app)

        resp = client.get("/api/ready")
        data = resp.json()
        assert "neo4j unavailable" in data["failures"]

    @pytest.mark.unit
    def test_ready_exempt_from_auth(self):
        """Ready is accessible without auth even when keys are configured."""
        app = FastAPI()
        app.include_router(router)
        app.state.runtime_ctx = _fake_runtime_ctx()
        wrapped = BearerAuthMiddleware(app, api_key="super-secret-admin-key")
        client = TestClient(wrapped)

        resp = client.get("/api/ready")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ready"

    @pytest.mark.unit
    def test_ready_json_serializable(self):
        """Response is JSON-serializable (no unserializable objects)."""
        app = FastAPI()
        app.include_router(router)
        app.state.runtime_ctx = _fake_runtime_ctx()
        client = TestClient(app)

        resp = client.get("/api/ready")
        json.dumps(resp.json())

    @pytest.mark.unit
    def test_ready_no_secrets_leaked(self):
        """Ready endpoint does not expose configured secrets."""
        settings = MemorySettings(
            api_host="127.0.0.1",
            api_key="super-secret-admin-key",
            agent_key="super-secret-agent-key",
            readonly_key="super-secret-readonly-key",
            neo4j_password="neo4j-super-secret-password",
            openai_api_key="openai-super-secret-key",
        )
        app = FastAPI()
        app.include_router(router)
        app.state.runtime_ctx = _fake_runtime_ctx()
        wrapped = BearerAuthMiddleware(
            app,
            api_key=settings.api_key,
            agent_key=settings.agent_key,
            readonly_key=settings.readonly_key,
        )
        client = TestClient(wrapped)

        resp = client.get("/api/ready")
        _check_no_secrets(resp.json())

    @pytest.mark.unit
    def test_ready_dev_mode_no_auth(self):
        """Ready works without any auth keys configured (dev mode)."""
        app = FastAPI()
        app.include_router(router)
        app.state.runtime_ctx = _fake_runtime_ctx()
        wrapped = BearerAuthMiddleware(app)
        client = TestClient(wrapped)

        resp = client.get("/api/ready")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ready"


# ===================================================================
# TestStats — GET /api/stats
# ===================================================================

class TestStats:
    """Contract tests for GET /api/stats."""

    @pytest.fixture
    def app_with_runtime(self):
        app = FastAPI()
        app.include_router(router)
        app.state.runtime_ctx = _fake_runtime_ctx()
        return app

    @pytest.mark.unit
    def test_stats_ok(self, app_with_runtime):
        """Returns 200 with expected fields when runtime and backend are ready."""
        app = app_with_runtime
        backend = _fake_backend()
        with patch.object(api_routes, "_get_backend", return_value=backend):
            client = TestClient(app)
            resp = client.get("/api/stats")

        assert resp.status_code == 200
        data = resp.json()
        assert data["queue_depth"] == 5
        assert data["enrichment_enabled"] is True
        assert data["since_hours"] == 24
        assert data["startup_mode"] == "full"
        assert "capabilities" in data
        assert "services" in data
        assert "enrichment" in data
        assert "operations" in data
        assert data["enrichment"]["total"] == 10

    @pytest.mark.unit
    def test_stats_503_when_no_runtime(self):
        """Returns 503 when runtime context is not initialized."""
        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)

        resp = client.get("/api/stats")
        assert resp.status_code == 503
        assert "runtime is not ready" in resp.json()["detail"]

    @pytest.mark.unit
    def test_stats_dev_mode_no_auth_required(self):
        """Stats works without auth token when no keys are configured."""
        app = FastAPI()
        app.include_router(router)
        app.state.runtime_ctx = _fake_runtime_ctx()
        wrapped = BearerAuthMiddleware(app)
        backend = _fake_backend()
        with patch.object(api_routes, "_get_backend", return_value=backend):
            client = TestClient(wrapped)
            resp = client.get("/api/stats")

        assert resp.status_code == 200

    @pytest.mark.unit
    def test_stats_requires_auth_when_keys_configured(self):
        """Returns 401 when auth keys are configured but no token is sent."""
        app = FastAPI()
        app.include_router(router)
        app.state.runtime_ctx = _fake_runtime_ctx()
        wrapped = BearerAuthMiddleware(app, api_key="valid-key")
        client = TestClient(wrapped)

        resp = client.get("/api/stats")
        assert resp.status_code == 401
        assert resp.json()["error"] == "Unauthorized"

    @pytest.mark.unit
    def test_stats_with_valid_token(self, app_with_runtime):
        """Returns 200 when a valid bearer token is provided (any tier)."""
        app = app_with_runtime
        wrapped = BearerAuthMiddleware(app, api_key="valid-key")
        backend = _fake_backend()
        with patch.object(api_routes, "_get_backend", return_value=backend):
            client = TestClient(wrapped)
            resp = client.get(
                "/api/stats",
                headers={"Authorization": "Bearer valid-key"},
            )

        assert resp.status_code == 200

    @pytest.mark.unit
    def test_stats_with_readonly_token(self, app_with_runtime):
        """Returns 200 when readonly token is provided."""
        app = app_with_runtime
        wrapped = BearerAuthMiddleware(
            app,
            operator_key="op-key",
            agent_key="ag-key",
            readonly_key="ro-key",
        )
        backend = _fake_backend()
        with patch.object(api_routes, "_get_backend", return_value=backend):
            client = TestClient(wrapped)
            resp = client.get(
                "/api/stats",
                headers={"Authorization": "Bearer ro-key"},
            )

        assert resp.status_code == 200

    @pytest.mark.unit
    def test_stats_rejects_invalid_token(self, app_with_runtime):
        """Returns 401 when an invalid bearer token is provided."""
        app = app_with_runtime
        wrapped = BearerAuthMiddleware(app, api_key="valid-key")
        client = TestClient(wrapped)

        resp = client.get(
            "/api/stats",
            headers={"Authorization": "Bearer wrong-key"},
        )
        assert resp.status_code == 401

    @pytest.mark.unit
    def test_stats_json_serializable(self, app_with_runtime):
        """Response is JSON-serializable (no unserializable objects)."""
        app = app_with_runtime
        backend = _fake_backend()
        with patch.object(api_routes, "_get_backend", return_value=backend):
            client = TestClient(app)
            resp = client.get("/api/stats")

        json.dumps(resp.json())

    @pytest.mark.unit
    def test_stats_with_tiered_keys_no_secrets_leaked(self, app_with_runtime):
        """Stats response does not expose configured auth keys or provider secrets."""
        settings = MemorySettings(
            api_host="127.0.0.1",
            api_key="super-secret-admin-key",
            agent_key="super-secret-agent-key",
            readonly_key="super-secret-readonly-key",
            neo4j_password="neo4j-super-secret-password",
            openai_api_key="openai-super-secret-key",
        )
        app = app_with_runtime
        wrapped = BearerAuthMiddleware(
            app,
            api_key=settings.api_key,
            agent_key=settings.agent_key,
            readonly_key=settings.readonly_key,
        )
        backend = _fake_backend()
        with patch.object(api_routes, "_get_backend", return_value=backend):
            client = TestClient(wrapped)
            resp = client.get(
                "/api/stats",
                headers={"Authorization": "Bearer super-secret-admin-key"},
            )

        _check_no_secrets(resp.json())

    @pytest.mark.unit
    def test_stats_services_shape(self, app_with_runtime):
        """Stats services payload contains expected service keys."""
        app = app_with_runtime
        backend = _fake_backend()
        with patch.object(api_routes, "_get_backend", return_value=backend):
            client = TestClient(app)
            resp = client.get("/api/stats")

        data = resp.json()
        services = data["services"]
        for svc in ("neo4j", "graphiti", "ingest", "recall", "scheduler"):
            assert svc in services, f"missing service key: {svc}"
            assert services[svc] in ("ok", "unavailable", "degraded", "queue_only"), (
                f"unexpected service status: {services[svc]}"
            )

    @pytest.mark.unit
    def test_stats_has_no_extra_unexpected_fields(self, app_with_runtime):
        """Stats response contains only the expected top-level fields."""
        app = app_with_runtime
        backend = _fake_backend()
        with patch.object(api_routes, "_get_backend", return_value=backend):
            client = TestClient(app)
            resp = client.get("/api/stats")

        expected = {
            "since_hours", "startup_mode", "capabilities", "services",
            "queue_depth", "enrichment_enabled", "scheduler",
            "enrichment", "operations",
        }
        actual = set(resp.json().keys())
        assert actual == expected, f"unexpected fields: {actual - expected}"


# ===================================================================
# TestOperatorDiagnosticsIndependence
# ===================================================================

class TestOperatorDiagnosticsIndependence:
    """Existing operator diagnostics remains independent and secret-safe."""

    @pytest.mark.unit
    def test_diagnostics_still_works(self):
        """build_operator_diagnostics produces expected shape."""
        from menhir.operator_diagnostics import build_operator_diagnostics

        settings = MemorySettings(api_host="127.0.0.1")
        d = build_operator_diagnostics(settings)
        assert "status" in d
        assert "bind" in d
        assert "auth" in d
        assert "safety" in d
        assert "checks" in d

    @pytest.mark.unit
    def test_diagnostics_no_secrets(self):
        """build_operator_diagnostics does not expose raw secret values."""
        from menhir.operator_diagnostics import build_operator_diagnostics

        settings = MemorySettings(
            api_host="127.0.0.1",
            api_key="super-secret-admin-key",
            agent_key="super-secret-agent-key",
            readonly_key="super-secret-readonly-key",
            neo4j_password="neo4j-super-secret-password",
            openai_api_key="openai-super-secret-key",
        )
        d = build_operator_diagnostics(settings)
        _check_no_secrets(d)
