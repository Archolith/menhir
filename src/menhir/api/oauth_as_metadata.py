"""OAuth authorization-server metadata endpoints for Menhir's embedded AS."""

from __future__ import annotations

from archolith_oauth import AuthorizationServerConfig, authorization_server_metadata
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from menhir.config import MemorySettings
from menhir.config.oauth import _as_bool, _get_setting, build_oauth_config

router = APIRouter()


def _as_enabled(settings: object) -> bool:
    return _as_bool(
        _get_setting(settings, "oauth_as_enabled", "MENHIR_OAUTH_AS_ENABLED", False)
    )


def build_authorization_server_config(settings: object) -> AuthorizationServerConfig:
    config = build_oauth_config(settings)
    base = config.public_base_url
    if not base:
        raise ValueError("MENHIR_PUBLIC_BASE_URL is required")
    if not config.resource:
        raise ValueError("MENHIR_OAUTH_RESOURCE or MENHIR_PUBLIC_BASE_URL is required")
    return AuthorizationServerConfig(
        issuer=base,
        resource=config.resource,
        scopes_supported=config.scopes_supported,
        default_scopes=config.scopes_supported,
        access_token_ttl_s=int(getattr(settings, "oauth_as_access_ttl_s", 3600)),
        authorization_code_ttl_s=int(getattr(settings, "oauth_as_code_ttl_s", 120)),
    )


@router.get("/.well-known/oauth-authorization-server", include_in_schema=False)
@router.get(
    "/.well-known/oauth-authorization-server/{_as_path:path}",
    include_in_schema=False,
)
async def oauth_authorization_server_metadata(
    request: Request,
    _as_path: str = "",
) -> JSONResponse:
    settings = getattr(request.app.state, "settings", None) or MemorySettings.from_env()
    if not _as_enabled(settings):
        raise HTTPException(
            status_code=404,
            detail="OAuth authorization-server metadata is not enabled",
        )
    try:
        config = build_authorization_server_config(settings)
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return JSONResponse(authorization_server_metadata(config))


__all__ = [
    "_as_enabled",
    "build_authorization_server_config",
    "oauth_authorization_server_metadata",
    "router",
]
