"""Client resolution (DCR store + CIMD) and request-parameter validation.

Extracted from :mod:`menhir.api.oauth_authorize` (behavior-preserving split):
the trusted redirect target resolution (exact-match, fail-closed), the CIMD
document-to-client conversion with bounded snapshot freshness, granted-scope
resolution, and PKCE/response-type validation.

``_cimd_resolver`` — the injectable resolver seam tests patch — stays on the
facade module; ``resolve_cimd_client`` reads it through a deferred facade
import at call time, which preserves that patch behavior exactly while keeping
this leaf free of module-level import cycles.
"""

from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import urlsplit

from archolith_oauth import resolve_client_metadata_document as _shared_cimd_resolver

from menhir.api.oauth_client_store import (
    OAuthClient,
    cimd_fetched_at,
    get_client_store,
    upsert_cimd_client,
)
from menhir.config.oauth import _get_setting

logger = logging.getLogger(__name__)

# Default bounded CIMD snapshot freshness: 24h.
_CIMD_DEFAULT_MAX_AGE_S = 86400
_MAX_CIMD_CLIENT_NAME_LEN = 255


class _RedirectError(Exception):
    """Raised once a trusted redirect_uri is established but the request is invalid."""

    def __init__(self, error: str, description: str) -> None:
        super().__init__(description)
        self.error = error
        self.description = description


def _is_https_url(value: str) -> bool:
    """True only for a well-formed HTTPS URL with a host (CIMD identifier shape)."""
    try:
        parts = urlsplit(value)
    except Exception:
        return False
    return parts.scheme == "https" and bool(parts.hostname)


def _stale_client_max_age_s(settings: object) -> int:
    """Bounded CIMD snapshot freshness (default 24h, shared with DCR reaping)."""
    return int(
        _get_setting(
            settings,
            "oauth_as_stale_client_max_age_s",
            "MENHIR_OAUTH_AS_STALE_CLIENT_MAX_AGE_S",
            _CIMD_DEFAULT_MAX_AGE_S,
        )
    )


def refresh_tokens_enabled(settings: object) -> bool:
    """True iff the AS issues refresh tokens (drives offline_access availability)."""
    from menhir.api.oauth_as_register import refresh_tokens_enabled as _flag

    return _flag(settings)


def _as_scopes_for_clients(settings: object) -> tuple[str, ...]:
    """Full configured AS scope surface for single-owner-profile CIMD clients;
    authorize still validates the requested subset. Includes offline_access only
    when refresh tokens are enabled."""
    from menhir.api.oauth_as_register import as_scope_surface

    return as_scope_surface(settings)


def _client_from_cimd_document(
    client_id: str, doc: Any, settings: object
) -> OAuthClient:
    """Convert resolver-validated metadata into an OAuthClient. Fails closed on
    any identity/shape/auth-method deviation."""
    from menhir.api.oauth_as_register import _redirect_uri_ok

    if not isinstance(doc, dict):
        raise ValueError("Client metadata document must be a JSON object")
    # Exact identity: the document's client_id must byte-match the URL used.
    if str(doc.get("client_id", "")) != client_id:
        raise ValueError("Metadata document client_id does not match the requesting identifier")
    redirect_uris_raw = doc.get("redirect_uris")
    if (
        not isinstance(redirect_uris_raw, list)
        or not redirect_uris_raw
        or not all(isinstance(u, str) and u and _redirect_uri_ok(u) for u in redirect_uris_raw)
    ):
        raise ValueError("Metadata document redirect_uris are invalid")
    auth_method = doc.get("token_endpoint_auth_method", "none")
    supported_auth_methods = doc.get("token_endpoint_auth_methods_supported")
    offers_public_client_auth = auth_method == "none" or (
        isinstance(supported_auth_methods, list)
        and all(isinstance(method, str) for method in supported_auth_methods)
        and "none" in supported_auth_methods
    )
    if not offers_public_client_auth:
        raise ValueError("Only public clients (token endpoint auth method 'none') are supported")
    client_name = str(doc.get("client_name", "")).strip()[:_MAX_CIMD_CLIENT_NAME_LEN]
    return OAuthClient(
        client_id=client_id,
        client_name=client_name,
        redirect_uris=tuple(str(u) for u in redirect_uris_raw),
        scopes=_as_scopes_for_clients(settings),
        client_secret_hash="",
        # Persist the method selected by this AS, not the client's preferred
        # default.  Current ChatGPT CIMD metadata prefers private_key_jwt but
        # explicitly offers both private_key_jwt and none.
        token_endpoint_auth_method="none",
        created_at=time.time(),
    )


async def resolve_cimd_client(client_id: str, settings: object) -> OAuthClient:
    """Resolve an HTTPS URL client_id via CIMD with durable bounded-freshness caching.

    A fresh cached snapshot is used without network; stale or missing snapshots
    trigger revalidation through the shared SSRF-safe resolver and are durably
    upserted under the exact URL client_id. Revalidation failure fails closed.
    Token/code exchange may still use the durable OAuthClient row after restart.
    """
    from menhir.api import oauth_authorize as _facade  # CIMD resolver seam (see docstring)
    from menhir.api.oauth_as_metadata import agent_smith_client_document_for_id

    local_document = agent_smith_client_document_for_id(client_id, settings)
    if local_document is not None:
        client = _client_from_cimd_document(client_id, local_document, settings)
        upsert_cimd_client(client, fetched_at=time.time())
        return client

    store = get_client_store()
    now = time.time()
    cached = store.get(client_id)
    fetched_at = cimd_fetched_at(client_id)
    max_age = _stale_client_max_age_s(settings)
    if (
        cached is not None
        and fetched_at is not None
        and max_age > 0
        and (now - fetched_at) <= max_age
    ):
        if cached.token_endpoint_auth_method != "none":
            raise ValueError("Cached client metadata is not a public client")
        return cached

    resolver = _facade._cimd_resolver or _shared_cimd_resolver
    try:
        doc = await resolver(client_id)
    except Exception as exc:
        logger.warning(
            "CIMD document retrieval or validation failed (%s)",
            type(exc).__name__,
        )
        raise ValueError("CIMD document could not be retrieved or validated") from exc
    client = _client_from_cimd_document(client_id, doc, settings)
    upsert_cimd_client(client, fetched_at=now)
    return client


async def _resolve_client_and_redirect(
    client_id: str, redirect_uri: str, settings: object
) -> OAuthClient:
    """Return the client iff it exists and *redirect_uri* exactly matches a registered
    URI. Ordinary persisted (DCR) client IDs resolve from the store; HTTPS URL
    client_ids resolve via the SSRF-safe CIMD path. Raises HTTPException-like
    signalling via ValueError for untrusted targets."""
    if not client_id:
        raise ValueError("Missing client_id")
    if _is_https_url(client_id):
        client = await resolve_cimd_client(client_id, settings)
    else:
        client = get_client_store().get(client_id)
    if client is None:
        raise ValueError("Unknown client_id")
    if not redirect_uri or redirect_uri not in client.redirect_uris:
        raise ValueError("redirect_uri does not match a registered redirect URI for this client")
    return client


def _resolve_scope(scope_raw: str, client: OAuthClient, settings: object) -> str:
    """Return the resolved, space-joined granted scope. Raises _RedirectError on a
    requested scope outside the client's grant."""
    currently_supported = set(_as_scopes_for_clients(settings))
    granted = set(client.scopes) & currently_supported
    if not scope_raw.strip():
        return " ".join(scope for scope in client.scopes if scope in granted)
    requested = [s for s in scope_raw.split() if s]
    for s in requested:
        if s not in granted:
            raise _RedirectError("invalid_scope", "Requested scope exceeds the client's granted scopes")
    return " ".join(requested)


def _validate_pkce_and_response(
    response_type: str, code_challenge: str, code_challenge_method: str
) -> None:
    if response_type != "code":
        raise _RedirectError("unsupported_response_type", "Only response_type=code is supported")
    if not code_challenge:
        raise _RedirectError("invalid_request", "code_challenge is required (PKCE)")
    if code_challenge_method != "S256":
        raise _RedirectError("invalid_request", "code_challenge_method must be S256")
