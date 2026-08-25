"""Regression coverage for immutable OAuth/HTTP settings snapshots."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from menhir.api.oauth import build_oauth_config
from menhir.api.oauth_authorize import _bad_request
from menhir.api.oauth_rate_limit import client_ip
from menhir.config import MemorySettings

pytestmark = pytest.mark.unit


def test_explicit_empty_snapshot_value_does_not_fall_through_to_env(monkeypatch):
    monkeypatch.setenv("MENHIR_PUBLIC_BASE_URL", "https://late-change.example")
    settings = MemorySettings(oauth_public_base_url="")

    assert build_oauth_config(settings).public_base_url == ""


def test_from_env_captures_oauth_and_http_settings(monkeypatch, tmp_path):
    monkeypatch.setenv("MENHIR_OAUTH_AS_ENABLED", "true")
    monkeypatch.setenv("MENHIR_PUBLIC_BASE_URL", "https://memory.example.com")
    monkeypatch.setenv("MENHIR_OAUTH_AS_DIR", str(tmp_path))
    monkeypatch.setenv("MENHIR_OAUTH_AS_ACCESS_TTL_S", "900")
    monkeypatch.setenv("MENHIR_OAUTH_AS_REGISTER_RATE", "7")
    monkeypatch.setenv("MENHIR_TRUSTED_PROXY", "yes")
    monkeypatch.setenv("MENHIR_TRUSTED_PROXY_PEERS", "127.0.0.1,10.0.0.2")
    monkeypatch.setenv("MENHIR_CORS_ORIGINS", "https://one.example, https://two.example")

    settings = MemorySettings.from_env()

    assert settings.oauth_as_enabled is True
    assert settings.oauth_as_access_ttl_s == 900
    assert settings.oauth_as_register_rate == 7
    assert settings.oauth_as_dir == str(tmp_path)
    assert settings.trusted_proxy is True
    assert settings.trusted_proxy_peers == ("127.0.0.1", "10.0.0.2")
    assert settings.cors_origins == ("https://one.example", "https://two.example")


def test_embedded_as_rejects_plain_http_public_url():
    with pytest.raises(ValueError, match="must use HTTPS"):
        MemorySettings(
            oauth_as_enabled=True,
            oauth_public_base_url="http://memory.example.com",
        )


def test_embedded_as_requires_public_url():
    with pytest.raises(ValueError, match="is required"):
        MemorySettings(oauth_as_enabled=True)


def test_embedded_as_allows_loopback_http_for_local_development():
    settings = MemorySettings(
        oauth_as_enabled=True,
        oauth_public_base_url="http://127.0.0.1:8100",
    )

    assert settings.oauth_as_enabled is True


@pytest.mark.parametrize(
    "public_base_url",
    [
        "https://user:secret@memory.example.com",
        "https://memory.example.com?tenant=secret",
        "https://memory.example.com#oauth",
    ],
)
def test_embedded_as_rejects_noncanonical_public_url_components(public_base_url):
    with pytest.raises(ValueError, match="credentials|query string|fragment"):
        MemorySettings(
            oauth_as_enabled=True,
            oauth_public_base_url=public_base_url,
        )


def test_embedded_as_canonicalizes_one_trailing_slash():
    settings = MemorySettings(
        oauth_as_enabled=True,
        oauth_public_base_url="https://memory.example.com/",
    )

    assert settings.oauth_public_base_url == "https://memory.example.com"


def test_untrusted_peer_cannot_supply_forwarded_rate_limit_identity():
    request = SimpleNamespace(
        client=SimpleNamespace(host="203.0.113.10"),
        headers={"x-forwarded-for": "198.51.100.20"},
    )
    settings = SimpleNamespace(
        trusted_proxy=True,
        trusted_proxy_peers=("127.0.0.1",),
    )

    assert client_ip(request, settings) == "203.0.113.10"


def test_consent_html_security_headers_are_fail_closed():
    response = _bad_request("invalid")

    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["referrer-policy"] == "no-referrer"


# ---------------------------------------------------------------------------
# CF-102: an explicitly-empty scope list must REVOKE, not silently restore the default.
# ---------------------------------------------------------------------------


def test_emptying_admin_scopes_actually_revokes_admin():
    """The asymmetry that made this a security defect: a privilege-ADDING override was
    honoured while a privilege-REMOVING one was discarded and `menhir:admin` left in force."""
    settings = MemorySettings(oauth_admin_scopes=())

    assert build_oauth_config(settings).admin_scopes == ()


def test_a_non_empty_scope_override_is_still_honoured():
    """Positive control for the test above: without this, an implementation that returned ()
    for everything would pass the revocation test while being completely broken."""
    settings = MemorySettings(
        oauth_scopes_supported=("menhir:read", "menhir:write", "custom:admin"),
        oauth_admin_scopes=("custom:admin",),
    )

    assert build_oauth_config(settings).admin_scopes == ("custom:admin",)


def test_unset_scopes_still_fall_back_to_the_built_in_default():
    """The fallback must survive: only None means unset, and a bare snapshot has no override."""
    assert build_oauth_config(SimpleNamespace()).admin_scopes == ("menhir:admin",)


def test_emptying_read_and_write_scopes_revokes_them_too():
    """The register filed this against admin only; read and write shared the defect."""
    settings = MemorySettings(oauth_read_scopes=(), oauth_write_scopes=())
    cfg = build_oauth_config(settings)

    assert cfg.read_scopes == ()
    assert cfg.write_scopes == ()


@pytest.mark.parametrize(
    "overrides",
    [
        {"oauth_scopes_supported": ("menhir:read", "offline_access")},
        {"oauth_read_scopes": ("offline_access",)},
        {"oauth_write_scopes": ("offline_access",)},
        {"oauth_admin_scopes": ("offline_access",)},
    ],
)
def test_offline_access_is_rejected_from_every_permission_setting(overrides):
    with pytest.raises(ValueError, match="protocol-only"):
        MemorySettings(**overrides)


def test_tier_scopes_must_be_supported_permission_scopes():
    with pytest.raises(ValueError, match="subset of oauth_scopes_supported"):
        MemorySettings(oauth_admin_scopes=("retired:admin",))


def test_revoked_admin_scope_denies_the_operator_tier():
    """Far-end assertion: config alone proves nothing, the tier mapping is what enforces."""
    from menhir.api.oauth import tier_from_scopes

    granted = build_oauth_config(SimpleNamespace())
    assert tier_from_scopes({"menhir:admin"}, granted) == "operator"  # control

    revoked = build_oauth_config(MemorySettings(oauth_admin_scopes=()))
    assert tier_from_scopes({"menhir:admin"}, revoked) != "operator"


# ---------------------------------------------------------------------------
# Phase 6: refresh-token grant settings (default off, env parsing, TTL > 0).
# ---------------------------------------------------------------------------


def test_refresh_grant_defaults_off_with_thirty_day_ttl():
    settings = MemorySettings()

    assert settings.oauth_as_refresh_tokens_enabled is False
    assert settings.oauth_as_refresh_ttl_s == 2592000


def test_refresh_grant_settings_parse_from_env(monkeypatch):
    monkeypatch.setenv("MENHIR_OAUTH_AS_REFRESH_TOKENS_ENABLED", "true")
    monkeypatch.setenv("MENHIR_OAUTH_AS_REFRESH_TTL_S", "86400")

    settings = MemorySettings.from_env()

    assert settings.oauth_as_refresh_tokens_enabled is True
    assert settings.oauth_as_refresh_ttl_s == 86400


def test_non_positive_refresh_ttl_is_rejected():
    with pytest.raises(ValueError, match="oauth_as_refresh_ttl_s"):
        MemorySettings(oauth_as_refresh_ttl_s=0)


def test_offline_access_never_grants_a_menhir_tier():
    from menhir.api.oauth import tier_from_scopes
    from menhir.config.oauth import build_oauth_config

    config = build_oauth_config(MemorySettings())
    assert tier_from_scopes({"offline_access"}, config) is None
    assert (
        tier_from_scopes(
            {"menhir:read", "menhir:write", "menhir:admin", "offline_access"},
            config,
        )
        == "operator"
    )
