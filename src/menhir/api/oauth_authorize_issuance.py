"""Shared authorization-code issuance for the OAuth AS authorize endpoint.

Extracted verbatim from :mod:`menhir.api.oauth_authorize` (behavior-preserving
split): the single code-issuing redirect shared by the one-click GET and the
POST approve path.
"""

from __future__ import annotations

from fastapi import HTTPException
from fastapi.responses import RedirectResponse

from menhir.api.auth_code_store import get_auth_code_store
from menhir.api.oauth_authorize_http import _as_issuer, _redirect
from menhir.config import build_oauth_config


def _issue_code_redirect(
    *,
    client_id: str,
    redirect_uri: str,
    scope: str,
    code_challenge: str,
    resource: str,
    state: str,
    subject: str,
    settings: object,
    consent_jti: str | None = None,
) -> RedirectResponse:
    """Issue a single-use code and 302 back to *redirect_uri* (shared by one-click GET
    and POST approve).

    ``resource`` is optional on the authorize request but mandatory on the code: the
    token endpoint requires it, and the exchange rejects a code whose bound resource
    does not match. A client that omits it is bound to this server's canonical
    resource so the code stays redeemable.
    """
    bound_resource = resource or build_oauth_config(settings).resource
    if not bound_resource:
        raise HTTPException(
            status_code=500,
            detail="MENHIR_OAUTH_RESOURCE or MENHIR_PUBLIC_BASE_URL is required",
        )
    code_store = get_auth_code_store()
    if consent_jti is None:
        code = code_store.issue(
            client_id=client_id,
            redirect_uri=redirect_uri,
            scope=scope,
            code_challenge=code_challenge,
            code_challenge_method="S256",
            resource=bound_resource,
            subject=subject,
        )
    else:
        code = code_store.issue_with_consent_nonce(
            jti=consent_jti,
            state=state,
            client_id=client_id,
            redirect_uri=redirect_uri,
            scope=scope,
            code_challenge=code_challenge,
            code_challenge_method="S256",
            resource=bound_resource,
            subject=subject,
        )
        if code is None:
            raise PermissionError(
                "Consent request has already been used, expired, changed, or is missing"
            )
    params = {"code": code, "iss": _as_issuer(settings)}
    if state:
        params["state"] = state
    return _redirect(redirect_uri, params)
