"""OAuth authorization-server metadata endpoints for Menhir's embedded AS."""

from __future__ import annotations

from archolith_oauth import AuthorizationServerConfig, authorization_server_metadata
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from menhir.config import MemorySettings
from menhir.config.oauth import _as_bool, _get_setting, build_oauth_config

router = APIRouter()

_REFRESH_TTL_DEFAULT_S = 30 * 24 * 60 * 60
_AGENT_SMITH_CLIENTS: dict[str, tuple[str, int]] = {
    "claude": ("Agent Smith - Claude", 43681),
    "codex": ("Agent Smith - Codex", 43682),
    "gemini": ("Agent Smith - Gemini", 43683),
    "antigravity-ide": ("Agent Smith - Antigravity IDE", 43684),
    "gemini-config": ("Agent Smith - Gemini Config", 43685),
    "opencode": ("Agent Smith - OpenCode", 43686),
    "qwen": ("Agent Smith - Qwen", 43687),
    "goose": ("Agent Smith - Goose", 43688),
    "reasonix": ("Agent Smith - Reasonix", 43689),
    "zcode": ("Agent Smith - ZCode", 43690),
    "cline": ("Agent Smith - Cline", 43691),
    "wsl-claude": ("Agent Smith - Claude (WSL)", 43692),
    "wsl-opencode": ("Agent Smith - OpenCode (WSL)", 43693),
}


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
        issue_refresh_tokens=_as_bool(
            getattr(settings, "oauth_as_refresh_tokens_enabled", False)
        ),
        refresh_token_ttl_s=int(
            getattr(settings, "oauth_as_refresh_ttl_s", _REFRESH_TTL_DEFAULT_S)
        ),
        authorization_response_iss_parameter_supported=True,
        client_id_metadata_document_supported=True,
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


@router.get(
    "/oauth/client-metadata/agent-smith.json",
    include_in_schema=False,
)
async def agent_smith_client_metadata(request: Request) -> JSONResponse:
    """Publish one stable public-client identity per Agent Smith connection.

    This document contains no credential or grant. Production authorization
    remains controlled by the immutable client policy and operator consent.
    """

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

    client_key = request.query_params.get("client", "")
    profile = _AGENT_SMITH_CLIENTS.get(client_key)
    if profile is None:
        raise HTTPException(status_code=404, detail="Unknown Agent Smith OAuth client")
    client_name, callback_port = profile
    client_id = (
        f"{config.issuer}/oauth/client-metadata/agent-smith.json?client={client_key}"
    )
    as_metadata = authorization_server_metadata(config)
    return JSONResponse(
        {
            "client_id": client_id,
            "client_name": client_name,
            "redirect_uris": [
                f"http://127.0.0.1:{callback_port}/oauth/callback",
                f"http://localhost:{callback_port}/oauth/callback",
            ],
            "grant_types": as_metadata["grant_types_supported"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
            "token_endpoint_auth_methods_supported": ["none"],
            "scope": " ".join(as_metadata["scopes_supported"]),
        }
    )


__all__ = [
    "_AGENT_SMITH_CLIENTS",
    "_as_enabled",
    "agent_smith_client_metadata",
    "build_authorization_server_config",
    "oauth_authorization_server_metadata",
    "router",
]
