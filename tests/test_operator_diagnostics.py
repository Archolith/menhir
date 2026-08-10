"""Offline tests for the operator diagnostics snapshot.

No server startup, no Neo4j, no Docker, no model calls.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import os

import pytest

from menhir.config import MemorySettings
from menhir.operator_diagnostics import build_operator_diagnostics


# ===================================================================
# Snapshot content and safety
# ===================================================================

def _check_no_secrets(d: dict) -> None:
    """Assert no raw secret values appear in the snapshot."""
    dumped = json.dumps(d)
    for secret in ("super-secret-key", "my-agent-token", "my-readonly-token",
                   "my-operator-token", "my-api-key-token"):
        assert secret not in dumped, f"secret leaked: {secret}"


class TestOperatorDiagnostics:
    @pytest.mark.unit
    def test_loopback_no_auth_is_warn_not_fail(self) -> None:
        """Safe local no-auth reports ok or warn, not fail."""
        settings = MemorySettings(api_host="127.0.0.1")
        d = build_operator_diagnostics(settings)
        assert d["status"] in ("ok", "warn")
        assert d["bind"]["loopback"] is True
        assert d["auth"]["mode"] == "no_auth"
        _check_no_secrets(d)

    @pytest.mark.unit
    def test_bearer_auth_reports_key_presence_without_values(self) -> None:
        """Bearer-auth snapshot reports key tier booleans, never the values."""
        settings = MemorySettings(
            api_host="0.0.0.0",
            api_key="super-secret-key",
            agent_key="my-agent-token",
            readonly_key="my-readonly-token",
            operator_key="my-operator-token",
        )
        d = build_operator_diagnostics(settings)
        assert d["auth"]["mode"] == "bearer_keys_configured"
        assert d["auth"]["agent_key_present"] is True
        assert d["auth"]["readonly_key_present"] is True
        assert d["auth"]["admin_key_present"] is True
        _check_no_secrets(d)

    @pytest.mark.unit
    def test_non_loopback_no_auth_fail_if_constructed_directly(self) -> None:
        """Non-loopback no-auth reports fail (this state is normally prevented)."""
        settings = MemorySettings(
            api_host="0.0.0.0",
            allow_insecure_remote_no_auth=True,
        )
        # Override the override so diagnostics sees the dangerous state
        object.__setattr__(settings, "allow_insecure_remote_no_auth", False)
        d = build_operator_diagnostics(settings)
        # The settings guard would normally block this, but diagnostics should
        # still report it correctly.
        assert d["status"] == "fail"
        assert d["bind"]["loopback"] is False
        assert any(c["name"] == "no_auth_remote_bind_guard" and c["status"] == "fail"
                   for c in d["checks"]), [c for c in d["checks"] if c["name"] == "no_auth_remote_bind_guard"]

    @pytest.mark.unit
    def test_insecure_override_reports_warn_names_override(self) -> None:
        """Insecure override reports warn and mentions the env var."""
        settings = MemorySettings(
            api_host="0.0.0.0",
            allow_insecure_remote_no_auth=True,
        )
        d = build_operator_diagnostics(settings)
        assert d["status"] == "warn"
        assert d["auth"]["insecure_remote_no_auth_override"] is True
        warn_checks = [c for c in d["checks"] if c["name"] == "insecure_remote_no_auth_override"]
        assert len(warn_checks) == 1
        assert "MENHIR_ALLOW_INSECURE_REMOTE_NO_AUTH" in warn_checks[0]["message"]
        assert d["safety"]["warnings"]
        _check_no_secrets(d)

    @pytest.mark.unit
    def test_json_output_booleans_no_secrets(self) -> None:
        """JSON output contains booleans, not secret values."""
        settings = MemorySettings(
            api_host="127.0.0.1",
            api_key="my-api-key-token",
        )
        d = build_operator_diagnostics(settings)
        dumped = json.dumps(d)
        assert '"agent_key_present": false' in dumped
        assert '"admin_key_present": false' in dumped
        assert '"mode": "bearer_keys_configured"' in dumped
        _check_no_secrets(d)

    @pytest.mark.unit
    def test_loopback_localhost(self) -> None:
        """localhost is recognized as loopback."""
        settings = MemorySettings(api_host="localhost")
        d = build_operator_diagnostics(settings)
        assert d["bind"]["loopback"] is True

    @pytest.mark.unit
    def test_loopback_ipv6(self) -> None:
        """::1 is recognized as loopback."""
        settings = MemorySettings(api_host="::1")
        d = build_operator_diagnostics(settings)
        assert d["bind"]["loopback"] is True

    @pytest.mark.unit
    def test_auth_remote_ok(self) -> None:
        """Auth keys present + non-loopback host is ok."""
        settings = MemorySettings(api_host="0.0.0.0", api_key="valid")
        d = build_operator_diagnostics(settings)
        assert d["status"] in ("ok", "warn")

    @pytest.mark.unit
    def test_agent_key_alone_sufficient(self) -> None:
        """Agent key alone should count as keys present."""
        settings = MemorySettings(api_host="0.0.0.0", agent_key="valid")
        d = build_operator_diagnostics(settings)
        assert d["auth"]["mode"] == "bearer_keys_configured"
        assert d["auth"]["agent_key_present"] is True

    @pytest.mark.unit
    def test_readonly_key_alone_sufficient(self) -> None:
        """Readonly key alone should count as keys present."""
        settings = MemorySettings(api_host="0.0.0.0", readonly_key="valid")
        d = build_operator_diagnostics(settings)
        assert d["auth"]["mode"] == "bearer_keys_configured"
        assert d["auth"]["readonly_key_present"] is True

    @pytest.mark.unit
    def test_oauth_enabled_owns_http_auth_and_disables_query_auth(self) -> None:
        """OAuth-enabled diagnostics should report OAuth as the active protected HTTP auth path."""
        settings = MemorySettings(api_host="127.0.0.1")
        object.__setattr__(settings, "oauth_enabled", True)
        object.__setattr__(settings, "oauth_public_base_url", "https://memory.example.com")
        object.__setattr__(settings, "oauth_authorization_servers", ("https://auth.example.com",))
        object.__setattr__(settings, "oauth_issuer", "https://auth.example.com/")
        object.__setattr__(settings, "oauth_jwks_uri", "https://auth.example.com/.well-known/jwks.json")
        object.__setattr__(settings, "oauth_audiences", ("https://memory.example.com/mcp-http",))
        d = build_operator_diagnostics(settings)
        assert d["auth"]["mode"] == "oauth_resource_server"
        assert d["auth"]["query_auth_enabled"] is False
        assert d["oauth_resource_server"]["status"] == "pass"
        query_auth_checks = [c for c in d["checks"] if c["name"] == "query_auth_status"]
        assert len(query_auth_checks) == 1
        assert "disabled while OAuth is enabled" in query_auth_checks[0]["message"]

    # -- S-001c/S-005: OAuth-aware diagnostics --

    @pytest.mark.unit
    def test_oauth_remote_bind_guard_passes(self) -> None:
        """OAuth-enabled + non-loopback + no keys: no_auth_remote_bind_guard is pass."""
        settings = MemorySettings(api_host="127.0.0.1")
        object.__setattr__(settings, "api_host", "0.0.0.0")
        object.__setattr__(settings, "oauth_enabled", True)
        object.__setattr__(settings, "oauth_public_base_url", "https://memory.example.com")
        object.__setattr__(settings, "oauth_authorization_servers", ("https://auth.example.com",))
        object.__setattr__(settings, "oauth_issuer", "https://auth.example.com/")
        object.__setattr__(settings, "oauth_jwks_uri", "https://auth.example.com/.well-known/jwks.json")
        d = build_operator_diagnostics(settings)
        guard_checks = [c for c in d["checks"] if c["name"] == "no_auth_remote_bind_guard"]
        assert len(guard_checks) == 1
        assert guard_checks[0]["status"] == "pass"
        assert "OAuth" in guard_checks[0]["message"]

    @pytest.mark.unit
    def test_oauth_remote_bind_allowed_is_false(self) -> None:
        """OAuth-enabled + non-loopback + no keys: safety.no_auth_remote_bind_allowed is False."""
        settings = MemorySettings(api_host="127.0.0.1")
        object.__setattr__(settings, "api_host", "0.0.0.0")
        object.__setattr__(settings, "oauth_enabled", True)
        object.__setattr__(settings, "oauth_public_base_url", "https://memory.example.com")
        object.__setattr__(settings, "oauth_authorization_servers", ("https://auth.example.com",))
        object.__setattr__(settings, "oauth_issuer", "https://auth.example.com/")
        object.__setattr__(settings, "oauth_jwks_uri", "https://auth.example.com/.well-known/jwks.json")
        d = build_operator_diagnostics(settings)
        assert d["safety"]["no_auth_remote_bind_allowed"] is False

    @pytest.mark.unit
    def test_no_oauth_no_keys_remote_bind_allowed_true(self) -> None:
        """No OAuth, no keys, non-loopback: no_auth_remote_bind_allowed is True (unchanged)."""
        settings = MemorySettings(api_host="0.0.0.0", allow_insecure_remote_no_auth=True)
        object.__setattr__(settings, "allow_insecure_remote_no_auth", False)
        d = build_operator_diagnostics(settings)
        assert d["safety"]["no_auth_remote_bind_allowed"] is True


# ===================================================================
# CLI integration
# ===================================================================

class TestCliDiagnostics:
    def _run_cli(self, *args: str) -> subprocess.CompletedProcess:
        repo_root = Path(__file__).resolve().parent.parent
        env = os.environ.copy()
        env.setdefault("PYTHONPATH", str(repo_root / "src"))
        return subprocess.run(
            [sys.executable, "-m", "menhir.cli", "diagnostics", *args],
            capture_output=True, text=True,
            env=env,
            cwd=repo_root,
        )

    @pytest.mark.unit
    def test_json_mode_emits_parseable_json(self) -> None:
        """JSON mode emits parseable JSON only."""
        result = self._run_cli("--json")
        assert result.returncode == 0, f"stderr: {result.stderr}"
        data = json.loads(result.stdout)
        assert "status" in data
        assert "bind" in data
        assert "auth" in data
        assert "safety" in data
        assert "checks" in data

    @pytest.mark.unit
    def test_human_mode_includes_status_host_port_auth_warnings(self) -> None:
        """Human mode includes status, host, port, auth mode, warnings."""
        result = self._run_cli()
        assert result.returncode == 0, f"stderr: {result.stderr}"
        stdout = result.stdout
        assert "Menhir operator diagnostics" in stdout
        assert "bind:" in stdout
        assert "auth:" in stdout
        assert "warnings:" in stdout

    @pytest.mark.unit
    def test_no_secrets_in_cli_output(self) -> None:
        """CLI output never contains raw key values."""
        result = self._run_cli("--json")
        data = json.loads(result.stdout)
        _check_no_secrets(data)


class TestASRateLimitProxyAwareness:
    """RL-001: warn that AS rate limits are peer-keyed on a loopback bind (proxy
    implied) unless trusted-proxy resolution is enabled."""

    def _check(self, d: dict) -> dict:
        return next(c for c in d["checks"] if c["name"] == "oauth_as_rate_limit_proxy_awareness")

    @pytest.mark.unit
    def test_warns_on_loopback_as_without_trusted_proxy(self, monkeypatch) -> None:
        monkeypatch.setenv("MENHIR_OAUTH_AS_ENABLED", "1")
        monkeypatch.delenv("MENHIR_TRUSTED_PROXY", raising=False)
        d = build_operator_diagnostics(
            MemorySettings(
                api_host="127.0.0.1",
                oauth_as_enabled=True,
                oauth_public_base_url="http://127.0.0.1:8100",
            )
        )
        assert self._check(d)["status"] == "warn"
        assert any("MENHIR_TRUSTED_PROXY" in w for w in d["safety"]["warnings"])

    @pytest.mark.unit
    def test_pass_when_trusted_proxy_enabled(self, monkeypatch) -> None:
        monkeypatch.setenv("MENHIR_OAUTH_AS_ENABLED", "1")
        monkeypatch.setenv("MENHIR_TRUSTED_PROXY", "1")
        d = build_operator_diagnostics(
            MemorySettings(
                api_host="127.0.0.1",
                oauth_as_enabled=True,
                oauth_public_base_url="http://127.0.0.1:8100",
                trusted_proxy=True,
            )
        )
        assert self._check(d)["status"] == "pass"

    @pytest.mark.unit
    def test_absent_when_as_disabled(self, monkeypatch) -> None:
        monkeypatch.delenv("MENHIR_OAUTH_AS_ENABLED", raising=False)
        d = build_operator_diagnostics(MemorySettings(api_host="127.0.0.1"))
        assert not any(
            c["name"] == "oauth_as_rate_limit_proxy_awareness" for c in d["checks"]
        )
