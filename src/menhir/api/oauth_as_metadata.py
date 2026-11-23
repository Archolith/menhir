"""OAuth authorization-server metadata endpoints for Menhir's embedded AS."""

from __future__ import annotations

from archolith_oauth import AuthorizationServerConfig, authorization_server_metadata
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from menhir.api.client_policy import ClientPolicyAuthority
from menhir.config import MemorySettings
from menhir.config.oauth import _as_bool, _get_setting, build_oauth_config

router = APIRouter()

_REFRESH_TTL_DEFAULT_S = 30 * 24 * 60 * 60
_AGENT_SMITH_METADATA_PATH = "/oauth/client-metadata/agent-smith.json"


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


def agent_smith_client_document_for_id(
    client_id: str,
    settings: object,
    client_policy: ClientPolicyAuthority | None = None,
) -> dict[str, object] | None:
    """Build a locally authoritative Agent Smith CIMD document for *client_id*.

    The deployment's digest-bound client policy owns these identities: a
    document exists only when the policy lists this exact client_id with a
    ``registration`` (client name and loopback redirect URIs). No policy, or
    no registration, means no document. Resolving them locally avoids an
    availability dependency on Cloudflare allowing the VPS to hairpin through
    its own public hostname. Every other HTTPS client_id still uses the shared
    SSRF-guarded outbound resolver.
    """

    config = build_authorization_server_config(settings)
    prefix = f"{config.issuer}{_AGENT_SMITH_METADATA_PATH}?client="
    if client_policy is None or not client_id.startswith(prefix):
        return None
    if not client_id[len(prefix) :]:
        return None
    policy = client_policy.clients.get(client_id)
    if policy is None or policy.registration is None:
        return None
    registration = policy.registration
    as_metadata = authorization_server_metadata(config)
    advertised_scopes = [
        scope
        for scope in as_metadata["scopes_supported"]
        if scope in policy.scopes or scope == "offline_access"
    ]
    return {
        "client_id": client_id,
        "client_name": registration.client_name,
        "redirect_uris": list(registration.redirect_uris),
        "grant_types": as_metadata["grant_types_supported"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        "token_endpoint_auth_methods_supported": ["none"],
        "scope": " ".join(advertised_scopes),
    }


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
    client_id = f"{config.issuer}{_AGENT_SMITH_METADATA_PATH}?client={client_key}"
    document = agent_smith_client_document_for_id(
        client_id,
        settings,
        getattr(request.app.state, "client_policy", None),
    )
    if document is None:
        raise HTTPException(status_code=404, detail="Unknown Agent Smith OAuth client")
    return JSONResponse(document)


__all__ = [
    "agent_smith_client_document_for_id",
    "_as_enabled",
    "agent_smith_client_metadata",
    "build_authorization_server_config",
    "oauth_authorization_server_metadata",
    "router",
]
