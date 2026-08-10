"""Contract tests for MCP backend-client hardening.

Covers:
- Backend URL resolution (explicit, env, default, normalization)
- Backend auth key selection (agent_key > api_key > none)
- Redacted diagnostics (booleans only, never raw keys)
- Request header construction (Authorization, user-agent headers)
- Failure message safety (no secrets in errors)
- Integration with operator diagnostics

No server startup, no Neo4j, no Docker, no model calls.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from menhir.api.routes import router
from menhir.config import MemorySettings
from menhir.mcp.service_access import (
    build_mcp_backend_diagnostics,
    redact_url_for_diagnostics,
    resolve_mcp_backend_auth_key,
    resolve_mcp_backend_url,
)


# ===================================================================
# Helpers
# ===================================================================

def _check_no_secrets(d: dict) -> None:
    """Assert no raw secret values appear in the JSON output."""
    dumped = json.dumps(d)
    for secret in (
        "super-secret-agent-key",
        "super-secret-api-key",
        "super-secret-readonly-key",
        "super-secret-url-password",
    ):
        assert secret not in dumped, f"secret leaked: {secret}"


# ===================================================================
# TestBackendUrlResolution
# ===================================================================

class TestBackendUrlResolution:
    """Backend URL resolution: explicit > env > default, trailing-slash normalization."""

    @pytest.mark.unit
    def test_explicit_url_wins(self):
        """Explicit URL takes priority over settings.backend_url."""
        settings = MemorySettings(backend_url="http://env-host:8080")
        url = resolve_mcp_backend_url(settings, explicit_url="http://explicit:9090")
        assert url == "http://explicit:9090"

    @pytest.mark.unit
    def test_settings_url_used_when_no_explicit(self):
        """settings.backend_url is used when no explicit URL is given."""
        settings = MemorySettings(backend_url="http://env-host:8080")
        url = resolve_mcp_backend_url(settings)
        assert url == "http://env-host:8080"

    @pytest.mark.unit
    def test_default_to_loopback_when_nothing_configured(self):
        """Falls back to http://api_host:api_port when nothing is set."""
        settings = MemorySettings(api_host="127.0.0.1", api_port=8100)
        url = resolve_mcp_backend_url(settings)
        assert url == "http://127.0.0.1:8100"

    @pytest.mark.unit
    def test_default_uses_settings_host_and_port(self):
        """Default URL uses api_host and api_port from settings."""
        settings = MemorySettings(api_host="127.0.0.1", api_port=9999)
        url = resolve_mcp_backend_url(settings)
        assert url == "http://127.0.0.1:9999"

    @pytest.mark.unit
    def test_trailing_slash_stripped(self):
        """Trailing slashes are stripped from the resolved URL."""
        settings = MemorySettings(backend_url="http://host:8080/api/")
        url = resolve_mcp_backend_url(settings)
        assert url == "http://host:8080/api"
        assert not url.endswith("/")

    @pytest.mark.unit
    def test_no_double_trailing_slash(self):
        """Already-clean URL stays clean."""
        settings = MemorySettings(backend_url="http://host:8080")
        url = resolve_mcp_backend_url(settings)
        assert url == "http://host:8080"

    @pytest.mark.unit
    def test_explicit_url_with_trailing_slash(self):
        """Explicit URL with trailing slash is normalized."""
        url = resolve_mcp_backend_url(explicit_url="http://explicit:9090/")
        assert url == "http://explicit:9090"


# ===================================================================
# TestBackendAuthKeySelection
# ===================================================================

class TestBackendAuthKeySelection:
    """Auth key selection: agent_key > api_key > none."""

    @pytest.mark.unit
    def test_agent_key_wins_over_api_key(self):
        """MENHIR_AGENT_KEY is preferred over MENHIR_API_KEY."""
        settings = MemorySettings(agent_key="super-secret-agent-key", api_key="super-secret-api-key")
        key = resolve_mcp_backend_auth_key(settings)
        assert key == "super-secret-agent-key"

    @pytest.mark.unit
    def test_api_key_used_when_no_agent_key(self):
        """MENHIR_API_KEY is used when agent_key is absent."""
        settings = MemorySettings(api_key="super-secret-api-key")
        key = resolve_mcp_backend_auth_key(settings)
        assert key == "super-secret-api-key"

    @pytest.mark.unit
    def test_no_key_returns_empty_string(self):
        """No configured keys results in empty string (dev mode)."""
        settings = MemorySettings()
        key = resolve_mcp_backend_auth_key(settings)
        assert key == ""

    @pytest.mark.unit
    def test_agent_key_empty_api_key_used(self):
        """Empty agent_key falls through to api_key."""
        settings = MemorySettings(agent_key="", api_key="fallback-key")
        key = resolve_mcp_backend_auth_key(settings)
        assert key == "fallback-key"

    @pytest.mark.unit
    def test_whitespace_only_keys_stripped(self):
        """Whitespace-only keys are treated as empty."""
        settings = MemorySettings(agent_key="   ", api_key="  ")
        key = resolve_mcp_backend_auth_key(settings)
        assert key == ""

    @pytest.mark.unit
    def test_whitespace_agent_key_falls_through_to_api_key(self):
        """Whitespace-only agent_key falls through to valid api_key."""
        settings = MemorySettings(agent_key="   ", api_key="valid-api")
        key = resolve_mcp_backend_auth_key(settings)
        assert key == "valid-api"

    @pytest.mark.unit
    def test_operator_key_not_used_for_backend_client(self):
        """operator_key does not affect backend-client auth selection."""
        settings = MemorySettings(operator_key="super-secret-operator", api_key="")
        key = resolve_mcp_backend_auth_key(settings)
        assert key == ""


# ===================================================================
# TestRedactedDiagnostics
# ===================================================================

class TestRedactedDiagnostics:
    """Diagnostics report key presence booleans but never raw keys."""

    @pytest.mark.unit
    def test_diagnostics_no_secrets_with_keys(self):
        """Diagnostics block does not leak configured keys."""
        settings = MemorySettings(
            backend_url="http://user:super-secret-url-password@backend:8099",
            agent_key="super-secret-agent-key",
            api_key="super-secret-api-key",
            readonly_key="super-secret-readonly-key",
        )
        d = build_mcp_backend_diagnostics(settings)
        _check_no_secrets(d)

    @pytest.mark.unit
    def test_diagnostics_reports_key_presence_booleans(self):
        """Key presence is reported as booleans, not values."""
        settings = MemorySettings(
            agent_key="super-secret-agent-key",
            api_key="super-secret-api-key",
        )
        d = build_mcp_backend_diagnostics(settings)
        assert d["agent_key_present"] is True
        assert d["api_key_present"] is True
        assert d["auth_header_will_be_sent"] is True

    @pytest.mark.unit
    def test_diagnostics_reports_no_keys(self):
        """No keys results in expected booleans."""
        settings = MemorySettings()
        d = build_mcp_backend_diagnostics(settings)
        assert d["agent_key_present"] is False
        assert d["api_key_present"] is False
        assert d["auth_header_will_be_sent"] is False

    @pytest.mark.unit
    def test_diagnostics_redacts_url_credentials(self):
        """URL with credentials is redacted."""
        settings = MemorySettings(
            backend_url="http://user:pass@backend:8099",
        )
        d = build_mcp_backend_diagnostics(settings)
        redacted = d["backend_url"]
        assert "user" not in redacted
        assert "pass" not in redacted
        assert "***:***" in redacted
        assert "backend:8099" in redacted

    @pytest.mark.unit
    def test_diagnostics_url_clean_no_redaction(self):
        """Clean URL without credentials is not modified."""
        settings = MemorySettings(backend_url="http://backend:8099")
        d = build_mcp_backend_diagnostics(settings)
        assert d["backend_url"] == "http://backend:8099"
        assert "***" not in d["backend_url"]

    @pytest.mark.unit
    def test_diagnostics_reports_enabled(self):
        """Enabled flag reflects backend_client_mode."""
        settings = MemorySettings(backend_url="http://backend:8099")
        d = build_mcp_backend_diagnostics(settings)
        assert d["enabled"] is True

    @pytest.mark.unit
    def test_diagnostics_reports_disabled(self):
        """Enabled flag is False when no backend URL configured."""
        settings = MemorySettings()
        d = build_mcp_backend_diagnostics(settings)
        assert d["enabled"] is False

    @pytest.mark.unit
    def test_diagnostics_warning_when_no_auth(self):
        """Warning is included when no bearer key is configured."""
        settings = MemorySettings(backend_url="http://backend:8099")
        d = build_mcp_backend_diagnostics(settings)
        assert len(d["warnings"]) == 1
        assert "No bearer key configured" in d["warnings"][0]

    @pytest.mark.unit
    def test_diagnostics_no_warning_when_auth_configured(self):
        """No warning when a bearer key is configured."""
        settings = MemorySettings(
            backend_url="http://backend:8099",
            agent_key="valid",
        )
        d = build_mcp_backend_diagnostics(settings)
        assert len(d["warnings"]) == 0

    @pytest.mark.unit
    def test_diagnostics_whitespace_agent_key_reports_not_present(self):
        """Whitespace-only agent_key is reported as not present."""
        settings = MemorySettings(
            backend_url="http://backend:8099",
            agent_key="   ",
            api_key="valid-api",
        )
        d = build_mcp_backend_diagnostics(settings)
        assert d["agent_key_present"] is False
        assert d["api_key_present"] is True
        assert d["auth_header_will_be_sent"] is True

    @pytest.mark.unit
    def test_diagnostics_no_warning_when_api_key_valid_agent_whitespace(self):
        """No warning when api_key is valid but agent_key is whitespace-only."""
        settings = MemorySettings(
            backend_url="http://backend:8099",
            agent_key="   ",
            api_key="valid-api",
        )
        d = build_mcp_backend_diagnostics(settings)
        assert len(d["warnings"]) == 0
        assert d["auth_header_will_be_sent"] is True

    @pytest.mark.unit
    def test_diagnostics_no_warning_when_backend_disabled(self):
        """No MCP no-key warning when backend-client mode is not enabled."""
        settings = MemorySettings()
        d = build_mcp_backend_diagnostics(settings)
        assert d["enabled"] is False
        assert len(d["warnings"]) == 0

    @pytest.mark.unit
    def test_diagnostics_json_serializable(self):
        """Diagnostics output is JSON-serializable."""
        settings = MemorySettings(
            backend_url="http://backend:8099",
            agent_key="valid",
        )
        d = build_mcp_backend_diagnostics(settings)
        json.dumps(d)


# ===================================================================
# TestRedactUrl
# ===================================================================

class TestRedactUrl:
    """URL redaction helper edge cases."""

    @pytest.mark.unit
    def test_redact_user_pass(self):
        """User and password are redacted."""
        result = redact_url_for_diagnostics("http://user:pass@host:8099/path")
        assert "user" not in result
        assert "pass" not in result
        assert "***:***" in result
        assert "host:8099/path" in result

    @pytest.mark.unit
    def test_redact_user_only(self):
        """User-only (no password) is handled."""
        result = redact_url_for_diagnostics("http://user@host:8099")
        assert "user" not in result
        assert "***:***" in result

    @pytest.mark.unit
    def test_no_credentials_unchanged(self):
        """URL without credentials is unchanged."""
        result = redact_url_for_diagnostics("http://host:8099")
        assert result == "http://host:8099"

    @pytest.mark.unit
    def test_empty_string(self):
        """Empty string is returned as-is."""
        result = redact_url_for_diagnostics("")
        assert result == ""

    @pytest.mark.unit
    def test_invalid_url_returns_same(self):
        """Invalid URL is returned unchanged."""
        result = redact_url_for_diagnostics("not-a-url")
        assert result == "not-a-url"


# ===================================================================
# TestBackendClientAuthHeader
# ===================================================================

class TestBackendClientAuthHeader:
    """BackendClient sends correct headers based on configured keys."""

    @pytest.mark.unit
    def test_agent_key_sent_as_bearer(self):
        """agent_key is sent as Authorization: Bearer <key>."""
        settings = MemorySettings(agent_key="super-secret-agent-key")
        # Use ASGI transport to avoid network
        app = FastAPI()
        app.include_router(router)
        transport = httpx.ASGITransport(app=app)

        from menhir.core.backend_impl import BackendClient
        client = BackendClient("http://test", settings=settings, client=httpx.AsyncClient(transport=transport))

        headers = client._default_headers()
        assert headers["Authorization"] == "Bearer super-secret-agent-key"

    @pytest.mark.unit
    def test_api_key_sent_when_no_agent_key(self):
        """api_key is sent when agent_key is absent."""
        settings = MemorySettings(api_key="super-secret-api-key")

        from menhir.core.backend_impl import BackendClient
        client = BackendClient("http://test", settings=settings)

        headers = client._default_headers()
        assert headers["Authorization"] == "Bearer super-secret-api-key"

    @pytest.mark.unit
    def test_no_auth_header_when_no_key(self):
        """No Authorization header when no key is configured."""
        settings = MemorySettings()

        from menhir.core.backend_impl import BackendClient
        client = BackendClient("http://test", settings=settings)

        headers = client._default_headers()
        assert "Authorization" not in headers

    @pytest.mark.unit
    def test_client_headers_sent(self):
        """x-yawn-* headers are sent from settings."""
        settings = MemorySettings(
            mcp_client_user_id="test-user",
            mcp_client_id="test-client-id",
            mcp_client_name="test-client",
        )

        from menhir.core.backend_impl import BackendClient
        client = BackendClient("http://test", settings=settings)

        headers = client._default_headers()
        assert headers["x-menhir-user-id"] == "test-user"
        assert headers["x-menhir-client-id"] == "test-client-id"
        assert headers["x-menhir-client-name"] == "test-client"


# ===================================================================
# TestFailureMessageSafety
# ===================================================================

class TestFailureMessageSafety:
    """Error messages are actionable and do not contain secrets."""

    @pytest.mark.unit
    def test_request_error_does_not_leak_key(self):
        """Failed request error message does not contain the key value."""
        from menhir.core.backend_impl import BackendClient

        settings = MemorySettings(agent_key="super-secret-agent-key")
        client = BackendClient("http://localhost:1", settings=settings)

        import asyncio
        try:
            asyncio.run(client._request("recall", {"query": "test"}))
        except Exception as exc:
            msg = str(exc)
            assert "super-secret-agent-key" not in msg
            assert "super-secret" not in msg

    @pytest.mark.unit
    def test_unauthorized_response_suggests_check_env(self):
        """Unauthorized response does not leak the key value."""
        from menhir.api.auth import BearerAuthMiddleware

        app = FastAPI()
        app.include_router(router)
        wrapped = BearerAuthMiddleware(app, api_key="server-key")

        async def _verify():
            transport = httpx.ASGITransport(app=wrapped)
            async with httpx.AsyncClient(transport=transport) as client:
                response = await client.post(
                    "http://test/api/internal/backend/recall",
                    json={"query": "test"},
                    headers={"Authorization": "Bearer wrong-key"},
                )
                assert response.status_code in (401,)
                text = response.text
                assert "wrong-key" not in text
                assert "super-secret" not in text

        import asyncio
        asyncio.run(_verify())


# ===================================================================
# TestOperatorDiagnosticsIntegration
# ===================================================================

class TestOperatorDiagnosticsIntegration:
    """MCP diagnostics integrates with operator diagnostics without leaking secrets."""

    @pytest.mark.unit
    def test_operator_diagnostics_includes_mcp_section(self):
        """Operator diagnostics includes mcp_backend_client section."""
        from menhir.operator_diagnostics import build_operator_diagnostics

        settings = MemorySettings(backend_url="http://backend:8099", agent_key="valid")
        d = build_operator_diagnostics(settings)
        assert "mcp_backend_client" in d
        assert d["mcp_backend_client"]["enabled"] is True
        assert d["mcp_backend_client"]["auth_header_will_be_sent"] is True

    @pytest.mark.unit
    def test_operator_diagnostics_no_secrets_in_mcp_section(self):
        """Operator diagnostics MCP section does not leak secrets."""
        from menhir.operator_diagnostics import build_operator_diagnostics

        settings = MemorySettings(
            backend_url="http://user:super-secret-url-password@backend:8099",
            agent_key="super-secret-agent-key",
            api_key="super-secret-api-key",
            readonly_key="super-secret-readonly-key",
        )
        d = build_operator_diagnostics(settings)
        _check_no_secrets(d)
        assert "***:***" in d["mcp_backend_client"]["backend_url"]

    @pytest.mark.unit
    def test_operator_diagnostics_disabled_mcp(self):
        """Operator diagnostics shows disabled MCP when no backend URL."""
        from menhir.operator_diagnostics import build_operator_diagnostics

        settings = MemorySettings()
        d = build_operator_diagnostics(settings)
        assert d["mcp_backend_client"]["enabled"] is False
