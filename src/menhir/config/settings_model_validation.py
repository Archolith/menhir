"""Validation for ``MemorySettings``.

Moved verbatim from ``menhir.config.settings_model.MemorySettings.__post_init__``.
The parameter is the settings instance (named ``self`` so the moved body stays
byte-identical); ``MemorySettings.__post_init__`` delegates here.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from .settings_helpers import assert_bind_safe, is_loopback_host

if TYPE_CHECKING:
    from menhir.config.settings_model import MemorySettings


def validate_memory_settings(self: "MemorySettings") -> None:
    """Validate bounds on critical numeric settings."""
    if self.graphiti_add_episode_timeout_seconds <= 0:
        raise ValueError(f"graphiti_add_episode_timeout_seconds must be > 0, got {self.graphiti_add_episode_timeout_seconds}")
    if self.api_port < 1 or self.api_port > 65535:
        raise ValueError(f"api_port must be 1-65535, got {self.api_port}")
    if self.max_llm_calls_per_session_window < 0:
        raise ValueError(f"max_llm_calls_per_session_window must be >= 0, got {self.max_llm_calls_per_session_window}")
    if self.max_llm_calls_per_enrichment_job < 0:
        raise ValueError(f"max_llm_calls_per_enrichment_job must be >= 0, got {self.max_llm_calls_per_enrichment_job}")
    if self.ingest_concurrency < 1:
        raise ValueError(f"ingest_concurrency must be >= 1, got {self.ingest_concurrency}")
    if self.llm_session_window_seconds < 1:
        raise ValueError(f"llm_session_window_seconds must be >= 1, got {self.llm_session_window_seconds}")
    if not 0 < self.personal_memory_scalar_threshold <= 1:
        raise ValueError(
            "personal_memory_scalar_threshold must be > 0 and <= 1, "
            f"got {self.personal_memory_scalar_threshold}"
        )
    if self.startup_scope not in {
        "full", "production", "auth-only", "http-only", "no-backend"
    }:
        raise ValueError(f"startup_scope is invalid: {self.startup_scope!r}")
    if self.runtime_mode not in {"production", "candidate-readonly"}:
        raise ValueError(f"runtime_mode is invalid: {self.runtime_mode!r}")
    if self.runtime_mode == "candidate-readonly" and self.startup_scope != "production":
        raise ValueError(
            "runtime_mode='candidate-readonly' requires startup_scope='production'"
        )
    positive_oauth_numbers = {
        "oauth_jwks_cache_ttl_s": self.oauth_jwks_cache_ttl_s,
        "oauth_http_timeout_s": self.oauth_http_timeout_s,
        "oauth_as_code_ttl_s": self.oauth_as_code_ttl_s,
        "oauth_as_access_ttl_s": self.oauth_as_access_ttl_s,
        "oauth_as_consent_ttl_s": self.oauth_as_consent_ttl_s,
        "oauth_as_session_ttl_s": self.oauth_as_session_ttl_s,
        "oauth_as_register_rate": self.oauth_as_register_rate,
        "oauth_as_register_window_s": self.oauth_as_register_window_s,
        "oauth_as_approve_rate": self.oauth_as_approve_rate,
        "oauth_as_approve_window_s": self.oauth_as_approve_window_s,
        "oauth_as_max_clients": self.oauth_as_max_clients,
        "oauth_as_refresh_ttl_s": self.oauth_as_refresh_ttl_s,
    }
    for name, value in positive_oauth_numbers.items():
        if value <= 0:
            raise ValueError(f"{name} must be > 0, got {value}")
    if self.oauth_clock_skew_s < 0:
        raise ValueError(f"oauth_clock_skew_s must be >= 0, got {self.oauth_clock_skew_s}")
    if self.oauth_as_stale_client_max_age_s < 0:
        raise ValueError(
            "oauth_as_stale_client_max_age_s must be >= 0, "
            f"got {self.oauth_as_stale_client_max_age_s}"
        )
    if not 0 <= self.oauth_as_refresh_retry_grace_s <= 60:
        raise ValueError(
            "oauth_as_refresh_retry_grace_s must be between 0 and 60 seconds, "
            f"got {self.oauth_as_refresh_retry_grace_s}"
        )
    if not self.oauth_allowed_algorithms or any(
        algorithm.lower() == "none" for algorithm in self.oauth_allowed_algorithms
    ):
        raise ValueError("oauth_allowed_algorithms must be non-empty and cannot include 'none'")
    if self.trusted_proxy and not self.trusted_proxy_peers:
        raise ValueError("trusted_proxy requires at least one trusted_proxy_peers entry")
    from .oauth import validate_permission_scope_config

    validate_permission_scope_config(
        scopes_supported=self.oauth_scopes_supported,
        read_scopes=self.oauth_read_scopes,
        write_scopes=self.oauth_write_scopes,
        admin_scopes=self.oauth_admin_scopes,
    )
    if self.oauth_as_enabled and not self.oauth_public_base_url:
        raise ValueError(
            "oauth_public_base_url is required when the embedded authorization server is enabled"
        )
    if self.oauth_as_enabled:
        normalized_base_url = self.oauth_public_base_url.strip().rstrip("/")
        object.__setattr__(self, "oauth_public_base_url", normalized_base_url)
        parsed = urlparse(normalized_base_url)
        host = (parsed.hostname or "").strip().lower()
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("oauth_public_base_url must not contain credentials")
        if parsed.query or parsed.fragment:
            raise ValueError(
                "oauth_public_base_url must not contain a query string or fragment"
            )
        if parsed.scheme != "https" and not (
            parsed.scheme == "http" and is_loopback_host(host)
        ):
            raise ValueError(
                "oauth_public_base_url must use HTTPS for the embedded authorization "
                "server (loopback HTTP is allowed for local development)"
            )
    if self.startup_scope == "production":
        expected_base = self.oauth_public_base_url.strip().rstrip("/")
        expected_resource = f"{expected_base}/mcp-http"
        expected_jwks = f"{expected_base}/.well-known/jwks.json"
        if not self.oauth_enabled or not self.oauth_as_enabled:
            raise ValueError(
                "production startup requires both OAuth resource-server and "
                "authorization-server support"
            )
        if not expected_base.startswith("https://"):
            raise ValueError("production OAuth authority must use an HTTPS origin")
        if self.oauth_resource != expected_resource:
            raise ValueError(
                "production OAuth resource must exactly match the public /mcp-http URL"
            )
        if self.oauth_issuer != expected_base:
            raise ValueError(
                "production OAuth issuer must exactly match the public origin"
            )
        if self.oauth_jwks_uri != expected_jwks:
            raise ValueError(
                "production OAuth JWKS URI must exactly match the public origin"
            )
        if not self.privacy_redact:
            raise ValueError("production startup requires privacy redaction")
        if not self.client_policy_path:
            raise ValueError("production startup requires an immutable client policy")
        if not self.oauth_signing_key_path:
            raise ValueError(
                "production startup requires an explicit OAuth signing key path"
            )
        if not Path(self.oauth_signing_key_path).is_absolute():
            raise ValueError("production OAuth signing key path must be absolute")
        if self.oauth_as_refresh_retry_grace_s > 0:
            if not self.oauth_refresh_retry_keyring_path:
                raise ValueError(
                    "production durable refresh retry requires an explicit keyring path"
                )
            if not Path(self.oauth_refresh_retry_keyring_path).is_absolute():
                raise ValueError(
                    "production refresh retry keyring path must be absolute"
                )
        if (
            len(self.client_policy_digest) != 64
            or any(ch not in "0123456789abcdef" for ch in self.client_policy_digest)
        ):
            raise ValueError(
                "production client policy digest must be a lowercase SHA-256 digest"
            )
    # Single source of truth: resolve the auth mode and enforce bind safety
    # through one path (assert_bind_safe -> resolve_auth_mode). Local import
    # avoids a circular import (menhir.api.oauth imports is_loopback_host).
    assert_bind_safe(self)
