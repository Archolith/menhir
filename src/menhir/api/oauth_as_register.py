"""Dynamic Client Registration endpoint (RFC 7591) for the embedded OAuth AS.

Public clients only (`token_endpoint_auth_method = "none"`, PKCE at authorize/token
time). No client secrets are issued. No outbound network I/O (the CIMD accept-path,
which fetches an external client-metadata URL, is a separate SSRF-guarded follow-on).
Unauthenticated by the DCR spec; gated by ``MENHIR_OAUTH_AS_ENABLED``.
"""

from __future__ import annotations

import logging
import time
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from menhir.api.oauth_as_metadata import _as_enabled
from menhir.api.oauth_client_store import OAuthClient, get_client_store, new_client_id
from menhir.api.oauth_rate_limit import FixedWindowLimiter, build_register_limiter, client_ip
from menhir.config import MemorySettings
from menhir.config.oauth import _get_setting, build_oauth_config
from menhir.config.settings import is_loopback_host

router = APIRouter()
logger = logging.getLogger(__name__)

# Emit a nearing-cap warning once the client table crosses this fraction of the
# cap, so operators notice a fill-up before DCR is refused (AS-002).
_NEARING_CAP_FRACTION = 0.8

_MAX_REDIRECT_URIS = 5
_MAX_CLIENT_NAME_LEN = 255
_SUPPORTED_GRANT_TYPES = {"authorization_code", "refresh_token"}
_SUPPORTED_RESPONSE_TYPES = {"code"}

# AS-002: unauthenticated open DCR is rate-limited per client IP and hard-capped in total,
# so an attacker cannot grow the client table without bound or amass attacker-controlled
# clients for the AS-001 abuse path.
_register_limiter = FixedWindowLimiter(max_per_window=20, window_s=600)
# Never-exchanged clients older than this are reaped before the cap is enforced,
# so an attacker cannot permanently brick DCR by filling the table with
# registrations that never complete a token exchange (AS-002). Default 24h.


def _redirect_uri_ok(uri: object) -> bool:
    """True only for an https URI, or an http URI whose host is loopback."""
    if not isinstance(uri, str) or not uri:
        return False
    try:
        parsed = urlparse(uri)
    except Exception:
        return False
    if parsed.scheme == "https":
        return bool(parsed.hostname)
    if parsed.scheme == "http":
        return is_loopback_host((parsed.hostname or "").strip().lower())
    return False


def _error(error: str, description: str) -> JSONResponse:
    """RFC 7591 error response (top-level ``error``/``error_description``)."""
    return JSONResponse(status_code=400, content={"error": error, "error_description": description})


@router.post("/oauth/register", include_in_schema=False)
async def register_client(request: Request) -> JSONResponse:
    settings = getattr(request.app.state, "settings", None) or MemorySettings.from_env()
    if not _as_enabled(settings):
        raise HTTPException(status_code=404, detail="OAuth dynamic client registration is not enabled")

    if not _register_limiter.allow(client_ip(request, settings)):
        return JSONResponse(
            status_code=429,
            content={
                "error": "temporarily_unavailable",
                "error_description": "Registration rate limit exceeded",
            },
        )
    # Opportunistically reap never-exchanged stale registrations so a slow
    # table-fill attack self-heals before it can reach the cap (AS-002).
    max_age = int(
        _get_setting(
            settings,
            "oauth_as_stale_client_max_age_s",
            "MENHIR_OAUTH_AS_STALE_CLIENT_MAX_AGE_S",
            86400,
        )
    )
    if max_age > 0:
        get_client_store().reap_stale(max_age)
    cap = int(
        _get_setting(
            settings,
            "oauth_as_max_clients",
            "MENHIR_OAUTH_AS_MAX_CLIENTS",
            1000,
        )
    )
    current = get_client_store().count()
    if current >= cap:
        return JSONResponse(
            status_code=429,
            content={
                "error": "temporarily_unavailable",
                "error_description": "Client registration limit reached",
            },
        )
    if current >= int(cap * _NEARING_CAP_FRACTION):
        logger.warning(
            "OAuth DCR client table nearing capacity: %d/%d registered clients "
            "(>= %d%% of MENHIR_OAUTH_AS_MAX_CLIENTS). New registrations will be "
            "refused at the cap; investigate for a registration-flood (AS-002).",
            current,
            cap,
            int(_NEARING_CAP_FRACTION * 100),
        )

    try:
        body = await request.json()
    except Exception:
        return _error("invalid_client_metadata", "Request body must be a JSON object")
    if not isinstance(body, dict):
        return _error("invalid_client_metadata", "Request body must be a JSON object")

    redirect_uris = body.get("redirect_uris")
    if not isinstance(redirect_uris, list) or not redirect_uris:
        return _error("invalid_client_metadata", "redirect_uris is required and must be a non-empty array")
    if len(redirect_uris) > _MAX_REDIRECT_URIS:
        return _error("invalid_client_metadata", f"At most {_MAX_REDIRECT_URIS} redirect_uris are allowed")
    for uri in redirect_uris:
        if not _redirect_uri_ok(uri):
            return _error("invalid_redirect_uri", "Each redirect_uri must be https or http to a loopback host")

    auth_method = body.get("token_endpoint_auth_method", "none")
    if auth_method != "none":
        return _error(
            "invalid_client_metadata",
            "Only token_endpoint_auth_method 'none' (public client + PKCE) is supported",
        )

    grant_types = body.get("grant_types")
    if grant_types is not None and (
        not isinstance(grant_types, list) or not set(grant_types) <= _SUPPORTED_GRANT_TYPES
    ):
        return _error("invalid_client_metadata", "Unsupported grant_types")

    response_types = body.get("response_types")
    if response_types is not None and (
        not isinstance(response_types, list) or not set(response_types) <= _SUPPORTED_RESPONSE_TYPES
    ):
        return _error("invalid_client_metadata", "Unsupported response_types")

    supported = tuple(build_oauth_config(settings).scopes_supported)
    requested_raw = body.get("scope")
    if requested_raw is None:
        granted = list(supported)
    elif isinstance(requested_raw, str):
        granted = [s for s in requested_raw.split() if s in supported]
    else:
        return _error("invalid_client_metadata", "scope must be a space-delimited string")

    client_name_raw = body.get("client_name", "")
    if not isinstance(client_name_raw, str):
        return _error("invalid_client_metadata", "client_name must be a string")
    client_name = client_name_raw.strip()[:_MAX_CLIENT_NAME_LEN]

    now = int(time.time())
    client_id = new_client_id()
    get_client_store().register(
        OAuthClient(
            client_id=client_id,
            client_name=client_name,
            redirect_uris=tuple(redirect_uris),
            scopes=tuple(granted),
            client_secret_hash="",
            created_at=float(now),
            token_endpoint_auth_method="none",
        )
    )

    return JSONResponse(
        status_code=201,
        content={
            "client_id": client_id,
            "client_id_issued_at": now,
            "redirect_uris": list(redirect_uris),
            "token_endpoint_auth_method": "none",
            # Only authorization_code is implemented at /oauth/token; do NOT advertise
            # refresh_token until it is (AS-005). The request-side _SUPPORTED_GRANT_TYPES
            # stays tolerant of a refresh_token in the registration body (harmless).
            "grant_types": ["authorization_code"],
            "response_types": ["code"],
            "client_name": client_name,
            "scope": " ".join(granted),
        },
    )
