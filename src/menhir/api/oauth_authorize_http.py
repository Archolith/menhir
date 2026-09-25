"""HTTP response helpers for the embedded OAuth AS authorization endpoint.

Extracted verbatim from :mod:`menhir.api.oauth_authorize` (behavior-preserving
split): the consent-page security headers, the exact-match redirect builder, AS
issuer resolution (RFC 9207), the protocol-error redirect, and the direct 400
error page used for untrusted targets.
"""

from __future__ import annotations

import html
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi.responses import HTMLResponse, RedirectResponse

_CONSENT_HEADERS = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
    "Content-Security-Policy": (
        "default-src 'none'; form-action 'self'; "
        "frame-ancestors 'none'; base-uri 'none'"
    ),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


def _consent_headers_for_redirect(redirect_uri: str) -> dict[str, str]:
    """Permit a validated callback origin through CSP's form redirect check.

    Chromium applies ``form-action`` to redirects produced by a form POST. The
    consent form posts to this server, then OAuth redirects to the client's
    exact registered callback. Keeping ``'self'`` alone therefore strands the
    issued code in the browser. Only the already-validated callback origin is
    added; the signed request and POST handler still enforce the exact URI.
    """

    parts = urlsplit(redirect_uri)
    if parts.scheme not in {"https", "http"} or not parts.netloc:
        raise ValueError("redirect_uri has no CSP-safe origin")
    origin = f"{parts.scheme}://{parts.netloc}"
    if any(character.isspace() for character in origin) or ";" in origin:
        raise ValueError("redirect_uri has no CSP-safe origin")
    headers = dict(_CONSENT_HEADERS)
    headers["Content-Security-Policy"] = (
        f"default-src 'none'; form-action 'self' {origin}; "
        "frame-ancestors 'none'; base-uri 'none'"
    )
    return headers


def _redirect(redirect_uri: str, params: dict[str, str]) -> RedirectResponse:
    """302 to *redirect_uri* with *params* merged into its query string."""
    parts = urlsplit(redirect_uri)
    query = parse_qsl(parts.query, keep_blank_values=True)
    query.extend((k, v) for k, v in params.items() if v != "")
    new_query = urlencode(query)
    target = urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, parts.fragment))
    return RedirectResponse(target, status_code=302)


def _as_issuer(settings: object) -> str:
    """Exact AS issuer (RFC 9207): must byte-match the advertised metadata issuer."""
    from menhir.api.oauth_as_metadata import build_authorization_server_config

    return build_authorization_server_config(settings).issuer


def _error_redirect(
    redirect_uri: str,
    error: str,
    description: str,
    state: str,
    settings: object,
) -> RedirectResponse:
    params = {"iss": _as_issuer(settings), "error": error, "error_description": description}
    if state:
        params["state"] = state
    return _redirect(redirect_uri, params)


def _bad_request(message: str) -> HTMLResponse:
    """Direct 400 (untrusted target — never redirect)."""
    body = (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        "<title>Authorization error</title></head><body>"
        "<h1>Authorization request rejected</h1><p>{}</p></body></html>"
    ).format(html.escape(message))
    return HTMLResponse(content=body, status_code=400, headers=_CONSENT_HEADERS)
