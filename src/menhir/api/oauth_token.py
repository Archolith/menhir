"""Token endpoint for Menhir's embedded OAuth authorization server."""

from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass
from typing import Any

from archolith_oauth import (
    TokenExchangeError,
    TokenIssuer,
    exchange_authorization_code,
    exchange_refresh_token,
)
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from menhir.api.auth_code_store import get_auth_code_store
from menhir.api.oauth_as_metadata import (
    _as_enabled,
    build_authorization_server_config,
)
from menhir.api.oauth_client_store import get_client_store
from menhir.api.oauth_keys import get_signing_key, public_jwks
from menhir.api.oauth_refresh_store import get_refresh_store
from menhir.config import MemorySettings

router = APIRouter()

_ACCESS_TTL_DEFAULT_S = 3600
_NO_STORE_HEADERS = {"Cache-Control": "no-store", "Pragma": "no-cache"}
_REFRESH_RETRY_GRACE_MAX_S = 60.0
_REFRESH_RETRY_CACHE_MAX_ENTRIES = 256
_REFRESH_RETRY_CACHE_MAX_PER_CLIENT = 32


@dataclass(frozen=True)
class _RefreshRetryEntry:
    expires_at: float
    response: Any
    client_id: str
    successor_digest: str | None


# A refresh response can be lost after the server commits single-use rotation but before the
# public client receives it (for example, a tunnel drops during the response). Retrying the same
# request would otherwise look exactly like token theft and revoke the replacement the client
# never received. Keep the complete response in process for a short, operator-bounded interval so
# an exact retry is idempotent. The cache key is a digest; raw presented tokens are never retained.
# The response necessarily contains the newly issued token, but only in memory and only for the
# configured grace interval. One lock covers lookup + rotation + insertion, making simultaneous
# requests in this AS process converge on the same response rather than racing the SQLite replay
# detector.
_refresh_retry_cache: dict[str, _RefreshRetryEntry] = {}
_refresh_retry_successors: dict[str, str] = {}
_refresh_retry_lock = threading.Lock()
_refresh_retry_cleanup_timer: threading.Timer | None = None
_refresh_retry_cleanup_deadline: float | None = None
_refresh_retry_cleanup_epoch = 0


def _settings_for(request: Request) -> object:
    return getattr(request.app.state, "settings", None) or MemorySettings.from_env()


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


def _refresh_retry_key(
    *, refresh_token: str, client_id: str, resource: str, scope: str | None
) -> str:
    digest = hashlib.sha256()
    parts = (refresh_token, client_id, resource)
    for part in parts:
        encoded = part.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    digest.update(b"\x01" if scope is None else b"\x00")
    if scope is not None:
        encoded_scope = scope.encode("utf-8")
        digest.update(len(encoded_scope).to_bytes(8, "big"))
        digest.update(encoded_scope)
    return digest.hexdigest()


def _refresh_token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _remove_refresh_retry_entry_locked(cache_key: str) -> None:
    entry = _refresh_retry_cache.pop(cache_key, None)
    if (
        entry is not None
        and entry.successor_digest is not None
        and _refresh_retry_successors.get(entry.successor_digest) == cache_key
    ):
        _refresh_retry_successors.pop(entry.successor_digest, None)


def _purge_expired_refresh_retries_locked(now: float) -> None:
    expired = [
        cache_key
        for cache_key, entry in _refresh_retry_cache.items()
        if entry.expires_at <= now
    ]
    for cache_key in expired:
        _remove_refresh_retry_entry_locked(cache_key)


def _expire_refresh_retry_entries(epoch: int) -> None:
    global _refresh_retry_cleanup_deadline, _refresh_retry_cleanup_timer

    with _refresh_retry_lock:
        if epoch != _refresh_retry_cleanup_epoch:
            return
        _refresh_retry_cleanup_timer = None
        _refresh_retry_cleanup_deadline = None
        _purge_expired_refresh_retries_locked(time.monotonic())
        _schedule_refresh_retry_cleanup_locked()


def _schedule_refresh_retry_cleanup_locked() -> None:
    global _refresh_retry_cleanup_deadline, _refresh_retry_cleanup_epoch
    global _refresh_retry_cleanup_timer

    if not _refresh_retry_cache:
        return
    earliest_expiry = min(entry.expires_at for entry in _refresh_retry_cache.values())
    if (
        _refresh_retry_cleanup_timer is not None
        and _refresh_retry_cleanup_timer.is_alive()
        and _refresh_retry_cleanup_deadline is not None
        and _refresh_retry_cleanup_deadline <= earliest_expiry
    ):
        return
    if _refresh_retry_cleanup_timer is not None:
        _refresh_retry_cleanup_timer.cancel()
    _refresh_retry_cleanup_epoch += 1
    epoch = _refresh_retry_cleanup_epoch
    timer = threading.Timer(
        max(0.0, earliest_expiry - time.monotonic()),
        _expire_refresh_retry_entries,
        args=(epoch,),
    )
    timer.daemon = True
    _refresh_retry_cleanup_timer = timer
    _refresh_retry_cleanup_deadline = earliest_expiry
    timer.start()


def _clear_refresh_retry_cache() -> None:
    """Clear cached credentials and cancel cleanup work during process/test teardown."""
    global _refresh_retry_cleanup_deadline, _refresh_retry_cleanup_epoch
    global _refresh_retry_cleanup_timer

    with _refresh_retry_lock:
        _refresh_retry_cleanup_epoch += 1
        if _refresh_retry_cleanup_timer is not None:
            _refresh_retry_cleanup_timer.cancel()
        _refresh_retry_cleanup_timer = None
        _refresh_retry_cleanup_deadline = None
        _refresh_retry_cache.clear()
        _refresh_retry_successors.clear()


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
        )

    key = _refresh_retry_key(
        refresh_token=refresh_token,
        client_id=client_id,
        resource=resource,
        scope=scope,
    )
    with _refresh_retry_lock:
        now = time.monotonic()
        _purge_expired_refresh_retries_locked(now)

        cached = _refresh_retry_cache.get(key)
        if cached is not None:
            return cached.response

        response = exchange_refresh_token(
            refresh_store=refresh_store,
            client_store=client_store,
            issuer=issuer,
            refresh_token=refresh_token,
            client_id=client_id,
            resource=resource,
            scope=scope,
            allowed_scopes=allowed_scopes,
        )

        # Once a successor is successfully presented, its predecessor response was received and
        # no longer needs retry protection. This keeps ordinary rapid rotations to one entry per
        # token family. Digests, rather than raw refresh tokens, back the successor index.
        predecessor_key = _refresh_retry_successors.get(
            _refresh_token_digest(refresh_token)
        )
        if predecessor_key is not None:
            _remove_refresh_retry_entry_locked(predecessor_key)

        client_entries = [
            (cache_key, entry)
            for cache_key, entry in _refresh_retry_cache.items()
            if entry.client_id == client_id
        ]
        if len(client_entries) >= _REFRESH_RETRY_CACHE_MAX_PER_CLIENT:
            oldest_client_key = min(
                client_entries,
                key=lambda item: item[1].expires_at,
            )[0]
            _remove_refresh_retry_entry_locked(oldest_client_key)
        if len(_refresh_retry_cache) >= _REFRESH_RETRY_CACHE_MAX_ENTRIES:
            oldest_key = min(
                _refresh_retry_cache,
                key=lambda cache_key: _refresh_retry_cache[cache_key].expires_at,
            )
            _remove_refresh_retry_entry_locked(oldest_key)

        successor = getattr(response, "refresh_token", None)
        successor_digest = (
            _refresh_token_digest(successor)
            if isinstance(successor, str) and successor
            else None
        )
        _refresh_retry_cache[key] = _RefreshRetryEntry(
            expires_at=time.monotonic() + grace_s,
            response=response,
            client_id=client_id,
            successor_digest=successor_digest,
        )
        if successor_digest is not None:
            _refresh_retry_successors[successor_digest] = key
        _schedule_refresh_retry_cleanup_locked()
        return response


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
                issue_refresh_without_offline_access=bool(
                    getattr(
                        settings,
                        "oauth_as_refresh_without_offline_access_enabled",
                        False,
                    )
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
