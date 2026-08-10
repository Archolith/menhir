"""Redacted operator diagnostics snapshot for local Menhir configuration.

Pure/offline — no network, no database, no server startup.
No secrets are exposed in the output.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from menhir.api.oauth import build_oauth_preflight
from menhir.api.oauth_as_metadata import _as_enabled
from menhir.config import (
    AuthMode,
    MemorySettings,
    build_oauth_config,
    resolve_auth_mode,
)
from menhir.config.settings import is_loopback_host
from menhir.mcp.service_access import build_mcp_backend_diagnostics


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class OperatorDiagnosticCheck:
    name: str
    status: str  # pass | warn | fail
    message: str


def _check_to_dict(c: OperatorDiagnosticCheck) -> dict[str, str]:
    return {"name": c.name, "status": c.status, "message": c.message}


# ---------------------------------------------------------------------------
# Snapshot builder
# ---------------------------------------------------------------------------

def build_operator_diagnostics(settings: MemorySettings) -> dict[str, Any]:
    """Build a redacted JSON-serializable diagnostics snapshot.

    Returns a dict (not a dataclass) for direct serialization.
    No secrets are included.
    """
    host = settings.api_host
    port = settings.api_port
    loopback = is_loopback_host(host)
    oauth_config = build_oauth_config(settings)
    oauth_preflight = build_oauth_preflight(oauth_config)
    oauth_enabled = bool(oauth_preflight.get("enabled"))

    auth_keys_present = bool(
        settings.api_key or settings.operator_key
        or settings.agent_key or settings.readonly_key
    )
    agent_key_present = bool(settings.agent_key)
    readonly_key_present = bool(settings.readonly_key)
    operator_key_present = bool(settings.operator_key)

    # Single source of truth for the effective auth mode — the same resolver the
    # middleware and bind guard use, so diagnostics can never report a mode the
    # server does not actually enforce.
    auth_mode = resolve_auth_mode(settings)
    _AUTH_MODE_LABEL = {
        AuthMode.OAUTH: "oauth_resource_server",
        AuthMode.CLIENT_TOKEN: "client_token_tier",
        AuthMode.STATIC: "bearer_keys_configured",
        AuthMode.NONE: "no_auth",
    }

    insecure_override = settings.allow_insecure_remote_no_auth

    # -- Compute checks --
    checks: list[dict[str, str]] = []
    warnings: list[str] = []

    # 1. bind_host_loopback
    if loopback:
        checks.append(_check_to_dict(OperatorDiagnosticCheck(
            name="bind_host_loopback",
            status="pass",
            message=f"Bind host {host!r} is a loopback address.",
        )))
    else:
        checks.append(_check_to_dict(OperatorDiagnosticCheck(
            name="bind_host_loopback",
            status="warn" if auth_keys_present or insecure_override else "fail",
            message=f"Bind host {host!r} is not a loopback address.",
        )))

    # 2. auth_keys_present
    if auth_keys_present:
        checks.append(_check_to_dict(OperatorDiagnosticCheck(
            name="auth_keys_present",
            status="pass",
            message=(
                "Bearer keys are configured but ignored on protected HTTP routes while OAuth is enabled."
                if oauth_enabled else
                "Bearer keys are configured."
            ),
        )))
    else:
        checks.append(_check_to_dict(OperatorDiagnosticCheck(
            name="auth_keys_present",
            status="pass" if oauth_enabled else "warn",
            message=(
                "No bearer keys configured — OAuth is expected to own protected HTTP auth."
                if oauth_enabled else
                "No bearer keys configured — running in open-auth mode."
            ),
        )))

    # 3. no_auth_remote_bind_guard
    if auth_mode.is_authenticated:
        checks.append(_check_to_dict(OperatorDiagnosticCheck(
            name="no_auth_remote_bind_guard",
            status="pass",
            message={
                AuthMode.OAUTH: "OAuth resource-server mode authenticates remote binds.",
                AuthMode.CLIENT_TOKEN: "Per-client token tier authenticates remote binds.",
                AuthMode.STATIC: "Auth keys present; remote bind allowed.",
            }[auth_mode],
        )))
    elif loopback:
        checks.append(_check_to_dict(OperatorDiagnosticCheck(
            name="no_auth_remote_bind_guard",
            status="pass",
            message="No-auth mode is restricted to loopback unless explicitly overridden.",
        )))
    elif insecure_override:
        checks.append(_check_to_dict(OperatorDiagnosticCheck(
            name="no_auth_remote_bind_guard",
            status="warn",
            message="No-auth mode allowed on non-loopback host via "
                    "MENHIR_ALLOW_INSECURE_REMOTE_NO_AUTH=1 override.",
        )))
        warnings.append("MENHIR_ALLOW_INSECURE_REMOTE_NO_AUTH is set — unsafe outside isolated lab networks.")
    else:
        checks.append(_check_to_dict(OperatorDiagnosticCheck(
            name="no_auth_remote_bind_guard",
            status="fail",
            message="No-auth mode would bind to a non-loopback host without override — "
                    "this state should be prevented by the settings guard.",
        )))

    # 4. insecure_remote_no_auth_override
    if insecure_override:
        checks.append(_check_to_dict(OperatorDiagnosticCheck(
            name="insecure_remote_no_auth_override",
            status="warn",
            message="MENHIR_ALLOW_INSECURE_REMOTE_NO_AUTH=1 is enabled — "
                    "no-key access allowed on non-loopback hosts.",
        )))
    else:
        checks.append(_check_to_dict(OperatorDiagnosticCheck(
            name="insecure_remote_no_auth_override",
            status="pass",
            message="Insecure remote no-auth override is not set.",
        )))

    # 5. query_auth_status
    checks.append(_check_to_dict(OperatorDiagnosticCheck(
        name="query_auth_status",
        status="pass",
        message=(
            "MCP query auth is disabled while OAuth is enabled."
            if oauth_enabled else
            "MCP query auth is available for ?api_key= on /mcp/ paths."
        ),
    )))

    # 6. destructive/admin key status — operator_key is the admin/destructive tier
    if operator_key_present:
        checks.append(_check_to_dict(OperatorDiagnosticCheck(
            name="admin_key_status",
            status="pass",
            message=(
                "Operator (admin) key is configured but ignored on protected HTTP routes while OAuth is enabled."
                if oauth_enabled else
                "Operator (admin) key is configured."
            ),
        )))
    else:
        checks.append(_check_to_dict(OperatorDiagnosticCheck(
            name="admin_key_status",
            status="info",
            message=(
                "No operator/admin key configured — OAuth scopes must authorize destructive HTTP operations."
                if oauth_enabled else
                "No operator/admin key configured — destructive operations "
                "may fall through to api_key or be unavailable."
            ),
        )))

    # 7. oauth_as_consent_secret (AS-003): multi-worker/-host determinism of the consent +
    # one-click HMAC secret. Surfaced only when the embedded AS is enabled.
    if _as_enabled(settings):
        if getattr(settings, "oauth_as_consent_secret", ""):
            checks.append(_check_to_dict(OperatorDiagnosticCheck(
                name="oauth_as_consent_secret",
                status="pass",
                message="MENHIR_OAUTH_AS_CONSENT_SECRET is set explicitly (stable across workers/hosts).",
            )))
        else:
            checks.append(_check_to_dict(OperatorDiagnosticCheck(
                name="oauth_as_consent_secret",
                status="warn",
                message=(
                    "MENHIR_OAUTH_AS_CONSENT_SECRET is not set. The consent/one-click HMAC "
                    "secret is derived from the on-disk signing key — stable for workers on the "
                    "same host, but multi-host horizontal scaling requires an explicit shared secret."
                ),
            )))
            warnings.append(
                "MENHIR_OAUTH_AS_CONSENT_SECRET is unset — set it explicitly before multi-host scaling."
            )

    # 8. oauth_as_rate_limit_proxy_awareness (RL-001): AS rate limits are keyed on
    # the peer IP. Behind a same-host reverse proxy (the standard topology for an
    # AS that needs an https public_base_url on a loopback bind) every external
    # caller shares the proxy's peer address, collapsing the per-IP DCR/approve
    # windows into one global window. Warn unless trusted-proxy resolution is on.
    if _as_enabled(settings):
        trusted_proxy = bool(getattr(settings, "trusted_proxy", False))
        if loopback and not trusted_proxy:
            checks.append(_check_to_dict(OperatorDiagnosticCheck(
                name="oauth_as_rate_limit_proxy_awareness",
                status="warn",
                message=(
                    "AS rate limits are keyed on the peer IP. On a loopback bind behind a "
                    "same-host reverse proxy, every caller shares one key and the per-IP DCR "
                    "and consent-approve limits become global. Set MENHIR_TRUSTED_PROXY=1 so "
                    "the real client is resolved from the proxy's X-Forwarded-For last hop."
                ),
            )))
            warnings.append(
                "AS rate limits are peer-keyed on a loopback bind — set MENHIR_TRUSTED_PROXY=1 "
                "if a reverse proxy fronts Menhir, or they can be evaded/DoSed as one global window."
            )
        else:
            checks.append(_check_to_dict(OperatorDiagnosticCheck(
                name="oauth_as_rate_limit_proxy_awareness",
                status="pass",
                message=(
                    "Trusted-proxy resolution is enabled; AS rate limits key on the forwarded client IP."
                    if trusted_proxy else
                    "AS rate limits key on the peer IP (a non-loopback bind implies no same-host proxy)."
                ),
            )))

    # -- Aggregate status --
    all_checks = list(checks)
    oauth_checks = oauth_preflight.get("checks")
    if isinstance(oauth_checks, list):
        all_checks.extend(c for c in oauth_checks if isinstance(c, dict))

    if any(c["status"] == "fail" for c in all_checks):
        overall_status = "fail"
    elif any(c["status"] == "warn" for c in all_checks):
        overall_status = "warn"
    else:
        overall_status = "ok"

    mcp_diag = build_mcp_backend_diagnostics(settings)

    return {
        "status": overall_status,
        "bind": {
            "host": host,
            "port": port,
            "loopback": loopback,
        },
        "auth": {
            "mode": _AUTH_MODE_LABEL[auth_mode],
            "agent_key_present": agent_key_present,
            "readonly_key_present": readonly_key_present,
            "admin_key_present": operator_key_present,
            "query_auth_enabled": not oauth_enabled,
            "insecure_remote_no_auth_override": insecure_override,
        },
        "safety": {
            # True only when the bind is genuinely unauthenticated on a
            # non-loopback host: auth mode is NONE AND not loopback. Any
            # authenticated mode (OAuth, client-token, static) is NOT unsafe.
            "no_auth_remote_bind_allowed": bool(
                not auth_mode.is_authenticated and not loopback
            ),
            "warnings": warnings,
        },
        "oauth_resource_server": oauth_preflight,
        "mcp_backend_client": mcp_diag,
        "checks": checks,
    }
