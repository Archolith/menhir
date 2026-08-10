"""OAuth protected-resource metadata endpoints for remote MCP clients."""

from __future__ import annotations

from archolith_oauth import ResourceServerConfig, protected_resource_metadata
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from menhir.api.oauth_as_metadata import _as_enabled
from menhir.api.oauth_keys import get_signing_key, public_jwks
from menhir.config import MemorySettings, build_oauth_config

router = APIRouter()


@router.get("/.well-known/jwks.json", include_in_schema=False)
async def jwks(request: Request) -> JSONResponse:
    settings = getattr(request.app.state, "settings", None)
    if settings is not None and not _as_enabled(settings):
        raise HTTPException(
            status_code=404,
            detail="OAuth authorization server is not enabled",
        )
    signing_key = (
        getattr(request.app.state, "oauth_signing_key", None) or get_signing_key()
    )
    return JSONResponse(public_jwks(signing_key))


@router.get("/.well-known/oauth-protected-resource", include_in_schema=False)
@router.get(
    "/.well-known/oauth-protected-resource/{_resource_path:path}",
    include_in_schema=False,
)
async def oauth_protected_resource_metadata(
    request: Request,
    _resource_path: str = "",
) -> JSONResponse:
    settings = getattr(request.app.state, "settings", None) or MemorySettings.from_env()
    config = build_oauth_config(settings)
    if not config.enabled:
        raise HTTPException(
            status_code=404,
            detail="OAuth protected-resource metadata is not enabled",
        )
    if not config.resource:
        raise HTTPException(
            status_code=500,
            detail="MENHIR_OAUTH_RESOURCE or MENHIR_PUBLIC_BASE_URL is required",
        )
    if not config.authorization_servers or not config.issuer or not config.jwks_uri:
        raise HTTPException(
            status_code=500,
            detail="OAuth authorization server, issuer, and JWKS URI are required",
        )

    resource = ResourceServerConfig(
        resource=config.resource,
        authorization_servers=config.authorization_servers,
        issuer=config.issuer,
        jwks_uri=config.jwks_uri,
        audiences=config.audiences,
        scopes_supported=config.scopes_supported,
        allowed_algorithms=config.allowed_algorithms,
        jwks_cache_ttl_s=config.jwks_cache_ttl_s,
        http_timeout_s=config.http_timeout_s,
        clock_skew_s=config.clock_skew_s,
        resource_name="Menhir MCP",
    )
    return JSONResponse(protected_resource_metadata(resource))


__all__ = [
    "jwks",
    "oauth_protected_resource_metadata",
    "router",
]
