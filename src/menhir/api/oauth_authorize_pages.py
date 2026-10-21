"""Consent-page HTML rendering for the embedded OAuth AS authorization endpoint.

Extracted verbatim from :mod:`menhir.api.oauth_authorize` (behavior-preserving
split): the hidden-input helper and the consent page that signs and registers a
single-use consent token. The tokenless retry page stays on the facade next to
the handlers (structural tests pin it there).
"""

from __future__ import annotations

import html

from menhir.api.oauth_authorize_tokens import (
    _SIGNED_FIELDS,
    _register_consent_jti,
    _sign_consent,
)
from menhir.api.oauth_client_store import OAuthClient


def _hidden(name: str, value: str) -> str:
    return '<input type="hidden" name="{}" value="{}">'.format(
        html.escape(name, quote=True), html.escape(value, quote=True)
    )


def _render_consent(
    fields: dict[str, str],
    client: OAuthClient,
    *,
    error: str = "",
    settings: object | None = None,
) -> str:
    consent_token = _sign_consent(fields, settings)
    _register_consent_jti(consent_token, fields, settings)
    scopes = fields.get("scope", "")
    error_html = (
        '<p style="color:#b00">{}</p>'.format(html.escape(error)) if error else ""
    )
    hidden_inputs = "".join(_hidden(k, fields.get(k, "")) for k in _SIGNED_FIELDS)
    hidden_inputs += _hidden("consent_token", consent_token)
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        "<title>Authorize connection</title></head><body>"
        "<h1>Authorize connection</h1>"
        "{error_html}"
        "<p><strong>{client_name}</strong> (<code>{client_id}</code>) is requesting access.</p>"
        "<p>Scopes: <code>{scopes}</code></p>"
        "<p>Codes will be returned to: <code>{redirect_uri}</code></p>"
        "<form method=\"post\" action=\"/oauth/authorize\">"
        "{hidden_inputs}"
        "<p><label>Admin secret: <input type=\"password\" name=\"admin_secret\" autocomplete=\"off\"></label></p>"
        "<button type=\"submit\" name=\"decision\" value=\"approve\">Approve</button> "
        "<button type=\"submit\" name=\"decision\" value=\"deny\">Deny</button>"
        "</form></body></html>"
    ).format(
        error_html=error_html,
        client_name=html.escape(client.client_name or client.client_id),
        client_id=html.escape(client.client_id),
        scopes=html.escape(scopes),
        redirect_uri=html.escape(fields.get("redirect_uri", "")),
        hidden_inputs=hidden_inputs,
    )
