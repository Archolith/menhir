"""Token endpoint for Menhir's embedded OAuth authorization server."""

from __future__ import annotations

from typing import Any

from archolith_oauth import (
    TokenExchangeError,
    TokenIssuer,
    exchange_authorization_code,
    exchange_refresh_token,
    exchange_refresh_token_durable,
)
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from menhir.api.auth_code_store import get_auth_code_store
from menhir.api.client_policy import ClientPolicy, ClientPolicyAuthority
from menhir.api.oauth_as_metadata import (
    _as_enabled,
    build_authorization_server_config,
)
from menhir.api.oauth_client_store import get_client_store
from menhir.api.oauth_keys import get_signing_key, public_jwks
from menhir.api.oauth_refresh_store import get_refresh_keyring, get_refresh_store
from menhir.config import MemorySettings

router = APIRouter()

_ACCESS_TTL_DEFAULT_S = 3600
_NO_STORE_HEADERS = {"Cache-Control": "no-store", "Pragma": "no-cache"}
_REFRESH_RETRY_GRACE_MAX_S = 60.0


def _settings_for(request: Request) -> object:
    return getattr(request.app.state, "settings", None) or MemorySettings.from_env()


def _require_production_client(
    request: Request,
    *,
    client_id: str,
    scopes: frozenset[str] | None = None,
) -> ClientPolicy | None:
    """Fail before code/refresh state changes when production policy denies a client."""

    authority = getattr(request.app.state, "client_policy", None)
    if authority is None:
        return None
    if not isinstance(authority, ClientPolicyAuthority):
        raise PermissionError("Production client policy authority is invalid")
    if scopes is None:
        return authority.policy_for_client_id(client_id)
    return authority.require_authorization(client_id=client_id, scopes=scopes)


def _access_ttl_s(settings: object | None = None) -> int:
    resolved = settings if settings is not None else MemorySettings.from_env()
    return int(getattr(resolved, "oauth_as_access_ttl_s", _ACCESS_TTL_DEFAULT_S))


def _refresh_retry_grace_s(settings: object) -> float:
    return max(
        0.0,
        min(
            _REFRESH_RETRY_GRACE_MAX_S,
            float(getattr(settings, "oauth_as_refresh_retry_grace_s", 0.0)),
        ),
    )


def _exchange_refresh_with_retry_grace(
    *,
    settings: object,
    refresh_store: Any,
    client_store: Any,
    issuer: TokenIssuer,
    refresh_token: str,
    client_id: str,
    resource: str,
    scope: str | None,
    allowed_scopes: Any,
    required_scopes: Any = None,
    tier: str | None = None,
) -> Any:
    """Rotate once and replay the exact response for a short lost-response window."""
    grace_s = _refresh_retry_grace_s(settings)
    if grace_s <= 0:
        return exchange_refresh_token(
            refresh_store=refresh_store,
            client_store=client_store,
            issuer=issuer,
            refresh_token=refresh_token,
            client_id=client_id,
            resource=resource,
            scope=scope,
            allowed_scopes=allowed_scopes,
            required_scopes=required_scopes,
            tier=tier,
        )

    return exchange_refresh_token_durable(
        refresh_store=refresh_store,
        client_store=client_store,
        issuer=issuer,
        keyring=get_refresh_keyring(),
        refresh_token=refresh_token,
        client_id=client_id,
        resource=resource,
        scope=scope,
        allowed_scopes=allowed_scopes,
        required_scopes=required_scopes,
        tier=tier,
    )


def _token_error(
    error: str,
    description: str,
    *,
    status_code: int = 400,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": error, "error_description": description},
        headers=_NO_STORE_HEADERS,
    )


def _signing_kid() -> str:
    return str(public_jwks(get_signing_key())["keys"][0]["kid"])


def _refresh_store_for(request: Request):
    """Return the immutable app-state store, with a legacy direct-adapter fallback."""
    if hasattr(request.app.state, "oauth_refresh_store"):
        return request.app.state.oauth_refresh_store
    return get_refresh_store()


@router.post("/oauth/token", include_in_schema=False)
async def token(request: Request) -> JSONResponse:
    settings = _settings_for(request)
    if not _as_enabled(settings):
        raise HTTPException(
            status_code=404,
            detail="OAuth token endpoint is not enabled",
        )

    form = await request.form()
    try:
        authorization_config = build_authorization_server_config(settings)
        issuer = TokenIssuer(authorization_config, get_signing_key())
        grant_type = str(form.get("grant_type", ""))

        if grant_type == "authorization_code":
            code = str(form.get("code", ""))
            redirect_uri = str(form.get("redirect_uri", ""))
            client_id = str(form.get("client_id", ""))
            code_verifier = str(form.get("code_verifier", ""))
            resource = str(form.get("resource", ""))
            if not (code and redirect_uri and client_id and code_verifier and resource):
                return _token_error(
                    "invalid_request",
                    "code, redirect_uri, client_id, code_verifier, and resource are required",
                )
            client_policy = _require_production_client(request, client_id=client_id)
            response = exchange_authorization_code(
                code_store=get_auth_code_store(),
                client_store=get_client_store(),
                refresh_store=_refresh_store_for(request),
                issuer=issuer,
                code=code,
                client_id=client_id,
                redirect_uri=redirect_uri,
                code_verifier=code_verifier,
                resource=resource,
                allowed_scopes=authorization_config.effective_scopes_supported,
                required_scopes=(
                    None if client_policy is None else client_policy.oauth_scopes
                ),
                issue_refresh_without_offline_access=bool(
                    getattr(
                        settings,
                        "oauth_as_refresh_without_offline_access_enabled",
                        False,
                    )
                ),
                tier=(
                    None if client_policy is None else client_policy.maximum_tier
                ),
            )
        elif grant_type == "refresh_token":
            if not authorization_config.issue_refresh_tokens:
                return _token_error(
                    "unsupported_grant_type",
                    "refresh_token grant is not enabled",
                )
            refresh_token = str(form.get("refresh_token", ""))
            client_id = str(form.get("client_id", ""))
            resource = str(form.get("resource", ""))
            if not (refresh_token and client_id and resource):
                return _token_error(
                    "invalid_request",
                    "refresh_token, client_id, and resource are required",
                )
            refresh_store = _refresh_store_for(request)
            if refresh_store is None:
                return _token_error(
                    "server_error",
                    "refresh token storage is not configured",
                    status_code=500,
                )
            raw_scope = form.get("scope")
            client_policy = _require_production_client(
                request,
                client_id=client_id,
                scopes=(
                    None
                    if raw_scope is None
                    else frozenset(str(raw_scope).split())
                ),
            )
            response = _exchange_refresh_with_retry_grace(
                settings=settings,
                refresh_store=refresh_store,
                client_store=get_client_store(),
                issuer=issuer,
                refresh_token=refresh_token,
                client_id=client_id,
                resource=resource,
                scope=None if raw_scope is None else str(raw_scope),
                allowed_scopes=authorization_config.effective_scopes_supported,
                required_scopes=(
                    None if client_policy is None else client_policy.oauth_scopes
                ),
                tier=(
                    None if client_policy is None else client_policy.maximum_tier
                ),
            )
        else:
            grants = (
                "authorization_code and refresh_token"
                if authorization_config.issue_refresh_tokens
                else "authorization_code"
            )
            return _token_error(
                "unsupported_grant_type",
                f"Only {grants} are supported",
            )
    except TokenExchangeError as exc:
        status = 500 if exc.error == "server_error" else 400
        return _token_error(exc.error, exc.description, status_code=status)
    except PermissionError as exc:
        return _token_error("unauthorized_client", str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return JSONResponse(
        status_code=200,
        content=response.as_dict(),
        headers=_NO_STORE_HEADERS,
    )


__all__ = [
    "_access_ttl_s",
    "_exchange_refresh_with_retry_grace",
    "_refresh_retry_grace_s",
    "_signing_kid",
    "_token_error",
    "_refresh_store_for",
    "router",
    "token",
]
