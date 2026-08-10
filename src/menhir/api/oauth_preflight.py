"""OAuth operator-preflight and URL-safety helpers."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse, urlunparse

from menhir.config.oauth import OAuthConfig
from menhir.config.settings import is_loopback_host


def _redact_url_credentials(url: str) -> str:
    """Redact userinfo from a URL.

    ``http://user:pass@host:8099/path`` → ``http://***:***@host:8099/path``
    """
    if not url:
        return url
    try:
        parsed = urlparse(url)
        if parsed.username or parsed.password:
            netloc = parsed.hostname or ""
            if parsed.port:
                netloc = f"{netloc}:{parsed.port}"
            redacted = parsed._replace(netloc=f"***:***@{netloc}")
            return urlunparse(redacted)
        return url
    except Exception:
        return url


def _check(name: str, status: str, message: str) -> dict[str, str]:
    return {"name": name, "status": status, "message": message}


def _url_has_credentials(url: str) -> bool:
    if not url:
        return False
    try:
        parsed = urlparse(url)
        return bool(parsed.username or parsed.password)
    except Exception:
        return False


def _is_https_or_loopback_http(url: str) -> bool:
    """Return True if *url* is HTTPS or HTTP to a loopback address."""
    if not url:
        return True
    try:
        parsed = urlparse(url)
        if parsed.scheme == "https":
            return True
        if parsed.scheme == "http":
            host = (parsed.hostname or "").strip().lower()
            return is_loopback_host(host)
        return True
    except Exception:
        return True


def _redact_url_in_message(message: str) -> str:
    """Redact credentials from any URLs found in a message string."""
    import re
    pattern = re.compile(r"(https?://)[^@\s]+@")
    return pattern.sub(r"\1***:***@", message)


def build_oauth_preflight(config: OAuthConfig) -> dict[str, object]:
    """Pure offline OAuth configuration preflight checks.

    No HTTP calls, no JWKS fetch, no IdP interaction.
    All URLs in the output are redacted of credentials.
    """
    if not config.enabled:
        return {
            "enabled": False,
            "status": "skip",
            "checks": [_check("oauth_enabled", "skip", "OAuth resource-server mode is disabled")],
        }

    checks: list[dict[str, str]] = []
    rollup_warnings = 0
    rollup_fails = 0

    # 1. public_base_url
    if config.public_base_url:
        if _url_has_credentials(config.public_base_url):
            checks.append(_check(
                "oauth_public_base_url_credentials",
                "warn",
                "MENHIR_PUBLIC_BASE_URL contains credentials (will be redacted in diagnostics).",
            ))
        if not _is_https_or_loopback_http(config.public_base_url):
            checks.append(_check(
                "oauth_public_base_url_scheme",
                "warn",
                "MENHIR_PUBLIC_BASE_URL uses http for a non-loopback host.",
            ))
        checks.append(_check(
            "oauth_public_base_url_configured",
            "pass",
            "MENHIR_PUBLIC_BASE_URL is configured.",
        ))
    else:
        checks.append(_check(
            "oauth_public_base_url_configured",
            "warn",
            "MENHIR_PUBLIC_BASE_URL is not set — metadata URL will use relative path.",
        ))

    # 2. resource
    if config.resource:
        if _url_has_credentials(config.resource):
            checks.append(_check(
                "oauth_resource_credentials",
                "warn",
                "MENHIR_OAUTH_RESOURCE contains credentials (will be redacted).",
            ))
        if not _is_https_or_loopback_http(config.resource):
            checks.append(_check(
                "oauth_resource_scheme",
                "warn",
                "MENHIR_OAUTH_RESOURCE uses http for a non-loopback host.",
            ))
        checks.append(_check("oauth_resource_configured", "pass", "OAuth resource is configured."))
    else:
        status = "fail" if not config.public_base_url else "pass"
        msg = "MENHIR_OAUTH_RESOURCE is not set and cannot be derived without MENHIR_PUBLIC_BASE_URL." if not config.public_base_url else "OAuth resource derived from MENHIR_PUBLIC_BASE_URL."
        checks.append(_check("oauth_resource_configured", status, msg))

    # 3. authorization_servers
    if config.authorization_servers:
        http_warnings = 0
        cred_warnings = 0
        for svr in config.authorization_servers:
            if _url_has_credentials(svr):
                cred_warnings += 1
            if not _is_https_or_loopback_http(svr):
                http_warnings += 1
        if cred_warnings:
            checks.append(_check(
                "oauth_authorization_servers_credentials",
                "warn",
                f"{cred_warnings} authorization server URL(s) contain credentials.",
            ))
        if http_warnings:
            checks.append(_check(
                "oauth_authorization_servers_scheme",
                "warn",
                f"{http_warnings} authorization server URL(s) use http for non-loopback host.",
            ))
        checks.append(_check(
            "oauth_authorization_servers_configured",
            "pass",
            f"{len(config.authorization_servers)} authorization server(s) configured.",
        ))
    else:
        checks.append(_check(
            "oauth_authorization_servers_configured",
            "fail",
            "MENHIR_AUTHORIZATION_SERVERS is required when OAuth is enabled.",
        ))

    # 4. issuer
    if config.issuer:
        if not config.issuer.endswith("/"):
            checks.append(_check(
                "oauth_issuer_trailing_slash",
                "warn",
                "MENHIR_OAUTH_ISSUER is missing a trailing slash — issuer matching may be strict.",
            ))
        checks.append(_check("oauth_issuer_configured", "pass", "OAuth issuer is configured."))
    else:
        checks.append(_check(
            "oauth_issuer_configured",
            "fail",
            "MENHIR_OAUTH_ISSUER is required when OAuth is enabled.",
        ))

    # 5. jwks_uri
    if config.jwks_uri:
        if _url_has_credentials(config.jwks_uri):
            checks.append(_check(
                "oauth_jwks_uri_credentials",
                "warn",
                "MENHIR_OAUTH_JWKS_URI contains credentials (will be redacted).",
            ))
        if not _is_https_or_loopback_http(config.jwks_uri):
            checks.append(_check(
                "oauth_jwks_uri_scheme",
                "warn",
                "MENHIR_OAUTH_JWKS_URI uses http for a non-loopback host.",
            ))
        checks.append(_check("oauth_jwks_uri_configured", "pass", "OAuth JWKS URI is configured."))
    else:
        checks.append(_check(
            "oauth_jwks_uri_configured",
            "fail",
            "MENHIR_OAUTH_JWKS_URI is required when OAuth is enabled.",
        ))

    # 6. audiences
    if config.audiences:
        checks.append(_check(
            "oauth_audience_configured",
            "pass",
            f"{len(config.audiences)} audience(s) configured.",
        ))
    else:
        checks.append(_check(
            "oauth_audience_configured",
            "fail",
            "No OAuth audience/resource configured — token binding will fail.",
        ))

    # 7. scopes
    if config.read_scopes and config.write_scopes:
        checks.append(_check(
            "oauth_scopes_configured",
            "pass",
            f"Read/write/admin scopes configured ({len(config.scopes_supported)} total).",
        ))
    else:
        checks.append(_check(
            "oauth_scopes_configured",
            "warn",
            "Menhir scopes may be incomplete — check MENHIR_OAUTH_READ/WRITE/ADMIN_SCOPES.",
        ))

    # 8. metadata_url consistency
    expected_path = "/.well-known/oauth-protected-resource"
    metadata_url = config.metadata_url
    if metadata_url.endswith(expected_path):
        checks.append(_check(
            "oauth_metadata_url_available",
            "pass",
            f"Protected-resource metadata available at {metadata_url}.",
        ))
    else:
        checks.append(_check(
            "oauth_metadata_url_available",
            "warn",
            f"Unexpected metadata URL format: {metadata_url}.",
        ))

    # 9. Bearer challenge consistency
    challenge = config.challenge()
    if metadata_url and f'resource_metadata="{metadata_url}"' in challenge:
        checks.append(_check(
            "oauth_challenge_metadata_consistent",
            "pass",
            "Bearer challenge includes correct resource_metadata.",
        ))
    else:
        checks.append(_check(
            "oauth_challenge_metadata_consistent",
            "warn",
            "Bearer challenge resource_metadata may not match metadata URL.",
        ))

    # Redact all check messages
    for c in checks:
        c["message"] = _redact_url_in_message(c["message"])

    # -- Aggregate status --
    rollup_fails = sum(1 for c in checks if c["status"] == "fail")
    rollup_warnings = sum(1 for c in checks if c["status"] == "warn")
    if rollup_fails:
        overall = "fail"
    elif rollup_warnings:
        overall = "warn"
    else:
        overall = "pass"

    return {
        "enabled": True,
        "status": overall,
        "metadata_url": _redact_url_credentials(metadata_url),
        "resource": _redact_url_credentials(config.resource),
        "authorization_servers_count": len(config.authorization_servers),
        "issuer_configured": bool(config.issuer),
        "jwks_uri_configured": bool(config.jwks_uri),
        "audiences_count": len(config.audiences),
        "scopes_supported_count": len(config.scopes_supported),
        "checks": checks,
    }
