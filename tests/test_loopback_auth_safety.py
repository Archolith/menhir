"""Offline unit tests for the loopback auth safety guard.

No server startup, no Neo4j, no Docker, no model calls.
"""

from __future__ import annotations

import pytest

from menhir.config.settings import (
    MemorySettings,
    is_loopback_host,
    validate_no_auth_bind_safety,
)


# ===================================================================
# is_loopback_host
# ===================================================================

class TestIsLoopbackHost:
    @pytest.mark.unit
    def test_127_0_0_1_is_loopback(self) -> None:
        assert is_loopback_host("127.0.0.1") is True

    @pytest.mark.unit
    def test_localhost_is_loopback(self) -> None:
        assert is_loopback_host("localhost") is True

    @pytest.mark.unit
    def test__1_is_loopback(self) -> None:
        assert is_loopback_host("::1") is True

    @pytest.mark.unit
    def test_0_0_0_0_is_not_loopback(self) -> None:
        assert is_loopback_host("0.0.0.0") is False

    @pytest.mark.unit
    def test_all_zero_ipv6_is_not_loopback(self) -> None:
        assert is_loopback_host("::") is False

    @pytest.mark.unit
    def test_lan_ip_is_not_loopback(self) -> None:
        assert is_loopback_host("192.168.1.1") is False

    @pytest.mark.unit
    def test_public_ip_is_not_loopback(self) -> None:
        assert is_loopback_host("10.0.0.5") is False

    @pytest.mark.unit
    def test_another_public_ip_is_not_loopback(self) -> None:
        assert is_loopback_host("203.0.113.42") is False


# ===================================================================
# validate_no_auth_bind_safety
# ===================================================================

class TestValidateNoAuthBindSafety:
    @pytest.mark.unit
    def test_loopback_no_auth_passes(self) -> None:
        validate_no_auth_bind_safety(
            host="127.0.0.1",
            auth_keys_present=False,
        )

    @pytest.mark.unit
    def test_localhost_no_auth_passes(self) -> None:
        validate_no_auth_bind_safety(
            host="localhost",
            auth_keys_present=False,
        )

    @pytest.mark.unit
    def test_ipv6_loopback_no_auth_passes(self) -> None:
        validate_no_auth_bind_safety(
            host="::1",
            auth_keys_present=False,
        )

    @pytest.mark.unit
    def test_0_0_0_0_no_auth_fails(self) -> None:
        with pytest.raises(ValueError, match="no-key/open-auth"):
            validate_no_auth_bind_safety(
                host="0.0.0.0",
                auth_keys_present=False,
            )

    @pytest.mark.unit
    def test_all_zero_ipv6_no_auth_fails(self) -> None:
        with pytest.raises(ValueError, match="no-key/open-auth"):
            validate_no_auth_bind_safety(
                host="::",
                auth_keys_present=False,
            )

    @pytest.mark.unit
    def test_lan_ip_no_auth_fails(self) -> None:
        with pytest.raises(ValueError, match="no-key/open-auth"):
            validate_no_auth_bind_safety(
                host="192.168.1.1",
                auth_keys_present=False,
            )

    @pytest.mark.unit
    def test_public_ip_no_auth_fails(self) -> None:
        with pytest.raises(ValueError, match="no-key/open-auth"):
            validate_no_auth_bind_safety(
                host="203.0.113.42",
                auth_keys_present=False,
            )

    @pytest.mark.unit
    def test_auth_key_present_allows_non_loopback(self) -> None:
        validate_no_auth_bind_safety(
            host="0.0.0.0",
            auth_keys_present=True,
        )

    @pytest.mark.unit
    def test_explicit_override_allows_non_loopback_no_auth(self) -> None:
        validate_no_auth_bind_safety(
            host="0.0.0.0",
            auth_keys_present=False,
            allow_insecure_remote_no_auth=True,
        )

    @pytest.mark.unit
    def test_error_message_mentions_loopback_and_env(self) -> None:
        with pytest.raises(ValueError) as exc_info:
            validate_no_auth_bind_safety(
                host="0.0.0.0",
                auth_keys_present=False,
            )
        msg = str(exc_info.value)
        assert "no-key" in msg or "open-auth" in msg
        assert "loopback" in msg or "127.0.0.1" in msg
        assert "MENHIR_ALLOW_INSECURE_REMOTE_NO_AUTH" in msg

    # -- S-001: OAuth-aware bind safety guard --

    @pytest.mark.unit
    def test_non_loopback_with_oauth_passes(self) -> None:
        """OAuth enabled allows non-loopback host without static keys."""
        validate_no_auth_bind_safety(
            host="0.0.0.0",
            auth_keys_present=False,
            oauth_enabled=True,
        )

    @pytest.mark.unit
    def test_non_loopback_no_oauth_still_fails(self) -> None:
        """OAuth disabled + no keys + non-loopback still raises."""
        with pytest.raises(ValueError, match="no-key/open-auth"):
            validate_no_auth_bind_safety(
                host="0.0.0.0",
                auth_keys_present=False,
                oauth_enabled=False,
            )

    @pytest.mark.unit
    def test_loopback_with_oauth_passes_silently(self) -> None:
        """Loopback host + OAuth enabled passes (short-circuit before raise)."""
        validate_no_auth_bind_safety(
            host="127.0.0.1",
            auth_keys_present=False,
            oauth_enabled=True,
        )


# ===================================================================
# Integration: MemorySettings enforces the guard in __post_init__
# ===================================================================

class TestMemorySettingsIntegration:
    @pytest.mark.unit
    def test_loopback_default_passes(self) -> None:
        """Default api_host is 127.0.0.1 and no auth keys — should pass."""
        settings = MemorySettings()
        assert settings.api_host == "127.0.0.1"

    @pytest.mark.unit
    def test_non_loopback_no_auth_fails_at_construction(self) -> None:
        """Non-loopback host without auth keys raises ValueError."""
        with pytest.raises(ValueError, match="no-key/open-auth"):
            MemorySettings(api_host="0.0.0.0")

    @pytest.mark.unit
    def test_non_loopback_with_auth_passes(self) -> None:
        """Auth key present allows non-loopback host."""
        settings = MemorySettings(api_host="0.0.0.0", api_key="secret")
        assert settings.api_host == "0.0.0.0"

    @pytest.mark.unit
    def test_non_loopback_with_agent_key_passes(self) -> None:
        """Agent key present allows non-loopback host."""
        settings = MemorySettings(api_host="0.0.0.0", agent_key="secret")
        assert settings.api_host == "0.0.0.0"

    @pytest.mark.unit
    def test_non_loopback_with_readonly_key_passes(self) -> None:
        """Readonly key present allows non-loopback host."""
        settings = MemorySettings(api_host="0.0.0.0", readonly_key="secret")
        assert settings.api_host == "0.0.0.0"

    @pytest.mark.unit
    def test_non_loopback_with_operator_key_passes(self) -> None:
        """Operator key present allows non-loopback host."""
        settings = MemorySettings(api_host="0.0.0.0", operator_key="secret")
        assert settings.api_host == "0.0.0.0"

    @pytest.mark.unit
    def test_non_loopback_with_override_passes(self) -> None:
        """Explicit insecure override allows non-loopback no-auth."""
        settings = MemorySettings(
            api_host="0.0.0.0",
            allow_insecure_remote_no_auth=True,
        )
        assert settings.api_host == "0.0.0.0"

    @pytest.mark.unit
    def test_non_loopback_from_env_no_auth_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """from_env raises when no auth and host is non-loopback."""
        monkeypatch.setenv("MENHIR_API_HOST", "0.0.0.0")
        monkeypatch.delenv("MENHIR_API_KEY", raising=False)
        monkeypatch.delenv("MENHIR_AGENT_KEY", raising=False)
        monkeypatch.delenv("MENHIR_OPERATOR_KEY", raising=False)
        monkeypatch.delenv("MENHIR_READONLY_KEY", raising=False)
        with pytest.raises(ValueError, match="no-key/open-auth"):
            MemorySettings.from_env()

    @pytest.mark.unit
    def test_non_loopback_from_env_with_override_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """from_env passes with explicit insecure override."""
        monkeypatch.setenv("MENHIR_API_HOST", "0.0.0.0")
        monkeypatch.setenv("MENHIR_ALLOW_INSECURE_REMOTE_NO_AUTH", "1")
        monkeypatch.delenv("MENHIR_API_KEY", raising=False)
        settings = MemorySettings.from_env()
        assert settings.api_host == "0.0.0.0"
        assert settings.allow_insecure_remote_no_auth is True
