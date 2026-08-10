"""Tests for API bearer auth middleware."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from menhir.api.auth import BearerAuthMiddleware
from menhir.api.oauth import OAuthAuthenticationError, OAuthConfig, OAuthPrincipal, _resource_matches
from menhir.mcp.service_access import get_request_auth_mode, get_request_session, get_request_tier


# ===================================================================
# Static API key tests
# ===================================================================

def _build_app(api_key: str) -> TestClient:
    app = FastAPI()

    @app.get("/api/health")
    async def health():
        return JSONResponse({"ok": True})

    @app.get("/api/secure")
    async def secure():
        return JSONResponse({"secure": True})

    @app.post("/api/internal/backend/test-op")
    async def internal_backend():
        return JSONResponse({"internal": True})

    @app.get("/api/query-echo")
    async def api_query_echo(api_key: str | None = None):
        return JSONResponse({"api_key": api_key})

    @app.get("/mcp")
    async def mcp_root(client_name: str | None = None, api_key: str | None = None):
        return JSONResponse({"client_name": client_name, "api_key": api_key, "root": True})

    @app.get("/mcp/test")
    async def mcp_test(client_name: str | None = None, api_key: str | None = None):
        return JSONResponse({"client_name": client_name, "api_key": api_key})

    @app.get("/mcp-http")
    async def mcp_http(client_name: str | None = None, api_key: str | None = None):
        return JSONResponse({"client_name": client_name, "api_key": api_key})

    @app.get("/other")
    async def other():
        return JSONResponse({"other": True})

    wrapped = BearerAuthMiddleware(app, api_key=api_key)
    return TestClient(wrapped)


def test_dev_mode_skips_auth_when_key_empty():
    client = _build_app("")
    resp = client.get("/api/secure")
    assert resp.status_code == 200
    assert resp.json()["secure"] is True


def test_health_is_exempt():
    client = _build_app("secret")
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_secure_route_requires_bearer_key():
    client = _build_app("secret")
    resp = client.get("/api/secure")
    assert resp.status_code == 401
    assert resp.json()["error"] == "Unauthorized"


def test_secure_route_accepts_valid_bearer_key():
    client = _build_app("secret")
    resp = client.get("/api/secure", headers={"Authorization": "Bearer secret"})
    assert resp.status_code == 200
    assert resp.json()["secure"] is True


def test_internal_backend_route_requires_bearer_key():
    client = _build_app("secret")
    resp = client.post("/api/internal/backend/test-op")
    assert resp.status_code == 401
    assert resp.json()["error"] == "Unauthorized"


def test_internal_backend_route_accepts_valid_bearer_key():
    client = _build_app("secret")
    resp = client.post("/api/internal/backend/test-op", headers={"Authorization": "Bearer secret"})
    assert resp.status_code == 200
    assert resp.json()["internal"] is True


def test_api_route_rejects_query_string_api_key():
    client = _build_app("secret")
    resp = client.get("/api/query-echo?api_key=secret")
    assert resp.status_code == 401
    assert resp.json()["error"] == "Unauthorized"


def test_mcp_root_route_requires_bearer_key():
    client = _build_app("secret")
    resp = client.get("/mcp")
    assert resp.status_code == 401
    assert resp.json()["error"] == "Unauthorized"


def test_mcp_route_accepts_query_string_api_key():
    client = _build_app("secret")
    resp = client.get("/mcp/test?api_key=secret&client_name=web-client")
    assert resp.status_code == 200
    assert resp.json() == {"client_name": "web-client", "api_key": None}


def test_mcp_root_accepts_query_string_api_key_and_scrubs_it():
    client = _build_app("secret")
    resp = client.get("/mcp?api_key=secret&client_name=web-client")
    assert resp.status_code == 200
    assert resp.json() == {"client_name": "web-client", "api_key": None, "root": True}


def test_mcp_http_route_requires_auth():
    client = _build_app("secret")
    resp = client.get("/mcp-http")
    assert resp.status_code == 401
    assert resp.json()["error"] == "Unauthorized"


def test_mcp_http_route_accepts_query_string_api_key_and_scrubs_it():
    client = _build_app("secret")
    resp = client.get("/mcp-http?api_key=secret&client_name=web-client")
    assert resp.status_code == 200
    assert resp.json() == {"client_name": "web-client", "api_key": None}


def test_non_api_route_is_not_protected():
    client = _build_app("secret")
    resp = client.get("/other")
    assert resp.status_code == 200
    assert resp.json()["other"] is True


# ── Tiered token tests ────────────────────────────────────────────────────────

def _build_tiered_app(
    *,
    operator_key: str = "",
    agent_key: str = "",
    readonly_key: str = "",
) -> TestClient:
    app = FastAPI()

    @app.get("/api/health")
    async def health():
        return JSONResponse({"ok": True})

    @app.get("/api/secure")
    async def secure():
        return JSONResponse({"secure": True})

    wrapped = BearerAuthMiddleware(
        app,
        operator_key=operator_key,
        agent_key=agent_key,
        readonly_key=readonly_key,
    )
    return TestClient(wrapped)


def test_resolve_tier_maps_all_keys():
    mw = BearerAuthMiddleware(
        FastAPI(),
        operator_key="op",
        agent_key="ag",
        readonly_key="ro",
    )
    assert mw._resolve_tier("op") == "operator"
    assert mw._resolve_tier("ag") == "agent"
    assert mw._resolve_tier("ro") == "readonly"
    assert mw._resolve_tier("unknown") is None
    assert mw._resolve_tier("") is None


def test_legacy_api_key_treated_as_operator():
    mw = BearerAuthMiddleware(FastAPI(), api_key="legacy")
    assert mw._resolve_tier("legacy") == "operator"
    assert mw.api_key == "legacy"


def test_tiered_operator_key_grants_access():
    client = _build_tiered_app(operator_key="op-key", agent_key="ag-key", readonly_key="ro-key")
    resp = client.get("/api/secure", headers={"Authorization": "Bearer op-key"})
    assert resp.status_code == 200


def test_tiered_agent_key_grants_access():
    client = _build_tiered_app(operator_key="op-key", agent_key="ag-key", readonly_key="ro-key")
    resp = client.get("/api/secure", headers={"Authorization": "Bearer ag-key"})
    assert resp.status_code == 200


def test_tiered_readonly_key_grants_access():
    client = _build_tiered_app(operator_key="op-key", agent_key="ag-key", readonly_key="ro-key")
    resp = client.get("/api/secure", headers={"Authorization": "Bearer ro-key"})
    assert resp.status_code == 200


def test_tiered_invalid_key_rejected():
    client = _build_tiered_app(operator_key="op-key", agent_key="ag-key")
    resp = client.get("/api/secure", headers={"Authorization": "Bearer wrong"})
    assert resp.status_code == 401


def test_tiered_dev_mode_when_all_keys_empty():
    client = _build_tiered_app()
    resp = client.get("/api/secure")
    assert resp.status_code == 200


# ── OAuth MCP resource-server tests ───────────────────────────────────────────

def _oauth_config() -> OAuthConfig:
    return OAuthConfig(
        enabled=True,
        public_base_url="https://memory.example.com",
        resource="https://memory.example.com/mcp-http",
        authorization_servers=("https://auth.example.com",),
        issuer="https://auth.example.com/",
        jwks_uri="https://auth.example.com/.well-known/jwks.json",
        audiences=("https://memory.example.com/mcp-http",),
    )


class _FakeOAuthVerifier:
    def __init__(self, principal: OAuthPrincipal | None = None, error: OAuthAuthenticationError | None = None) -> None:
        self.principal = principal
        self.error = error
        self.seen_token = ""

    async def verify_access_token(self, token: str) -> OAuthPrincipal:
        self.seen_token = token
        if self.error is not None:
            raise self.error
        assert self.principal is not None
        return self.principal


def _build_oauth_app(verifier: _FakeOAuthVerifier, *, static_api_key: str = "") -> TestClient:
    app = FastAPI()

    @app.get("/api/secure")
    async def secure():
        session = get_request_session()
        return JSONResponse(
            {
                "auth_mode": get_request_auth_mode(),
                "client_id": session.client_id if session else "",
                "client_name": session.client_name if session else "",
                "tier": get_request_tier(),
                "user_id": session.user_id if session else "",
            }
        )

    @app.get("/mcp")
    async def mcp_root(client_name: str | None = None, api_key: str | None = None):
        session = get_request_session()
        return JSONResponse(
            {
                "api_key": api_key,
                "auth_mode": get_request_auth_mode(),
                "client_id": session.client_id if session else "",
                "client_name": session.client_name if session else "",
                "client_name_query": client_name,
                "tier": get_request_tier(),
                "user_id": session.user_id if session else "",
            }
        )

    @app.get("/mcp/test")
    async def mcp_test(client_name: str | None = None, api_key: str | None = None):
        session = get_request_session()
        return JSONResponse(
            {
                "api_key": api_key,
                "auth_mode": get_request_auth_mode(),
                "client_id": session.client_id if session else "",
                "client_name": session.client_name if session else "",
                "client_name_query": client_name,
                "tier": get_request_tier(),
                "user_id": session.user_id if session else "",
            }
        )

    wrapped = BearerAuthMiddleware(
        app,
        api_key=static_api_key,
        oauth_config=_oauth_config(),
        oauth_verifier=verifier,
    )
    return TestClient(wrapped)


def test_oauth_disabled_preserves_static_auth():
    """OAuth disabled: static bearer auth still works for API and MCP paths."""
    app = FastAPI()

    @app.get("/api/secure")
    async def secure():
        return JSONResponse({"secure": True})

    @app.get("/mcp/test")
    async def mcp_test():
        return JSONResponse({"ok": True})

    wrapped = BearerAuthMiddleware(
        app,
        api_key="static-key",
        oauth_config=OAuthConfig(enabled=False),
    )
    client = TestClient(wrapped)

    resp = client.get("/api/secure", headers={"Authorization": "Bearer static-key"})
    assert resp.status_code == 200

    resp = client.get("/mcp/test?api_key=static-key")
    assert resp.status_code == 200


def test_oauth_mcp_root_rejects_missing_header_and_advertises_metadata():
    """OAuth enabled: /mcp root requires bearer, ?api_key= is rejected."""
    verifier = _FakeOAuthVerifier(
        OAuthPrincipal(subject="user-123", client_id="chatgpt", client_name="ChatGPT", tier="readonly")
    )
    client = _build_oauth_app(verifier, static_api_key="legacy")

    resp = client.get("/mcp?api_key=legacy&client_name=web-client")

    assert resp.status_code == 401
    assert resp.json()["code"] == "invalid_token"
    challenge = resp.headers["www-authenticate"]
    assert challenge.startswith('Bearer resource_metadata=')
    assert 'error="invalid_token"' in challenge
    assert verifier.seen_token == ""


def test_oauth_mcp_rejects_missing_header_and_advertises_metadata():
    """OAuth enabled: /mcp/test requires bearer, ?api_key= is rejected."""
    verifier = _FakeOAuthVerifier(
        OAuthPrincipal(subject="user-123", client_id="chatgpt", client_name="ChatGPT", tier="readonly")
    )
    client = _build_oauth_app(verifier, static_api_key="legacy")

    resp = client.get("/mcp/test?api_key=legacy&client_name=web-client")

    assert resp.status_code == 401
    assert resp.json()["code"] == "invalid_token"
    challenge = resp.headers["www-authenticate"]
    assert challenge.startswith('Bearer resource_metadata=')
    assert 'error="invalid_token"' in challenge
    assert verifier.seen_token == ""


def test_oauth_mcp_accepts_valid_token_and_binds_scope_tier_identity():
    """Valid token with read scope maps to readonly tier and binds identity from claims."""
    verifier = _FakeOAuthVerifier(
        OAuthPrincipal(
            subject="user-123",
            client_id="chatgpt-client",
            client_name="ChatGPT",
            scopes=frozenset({"menhir:read"}),
            tier="readonly",
        )
    )
    client = _build_oauth_app(verifier)

    resp = client.get(
        "/mcp/test?client_name=query-name",
        headers={"Authorization": "Bearer oauth-token"},
    )

    assert resp.status_code == 200
    assert verifier.seen_token == "oauth-token"
    assert resp.json() == {
        "api_key": None,
        "auth_mode": "oauth",
        "client_id": "chatgpt-client",
        "client_name": "ChatGPT",
        "client_name_query": "query-name",
        "tier": "readonly",
        "user_id": "user-123",
    }


def test_oauth_mcp_invalid_token_returns_bearer_challenge():
    """Invalid token returns 401 with WWW-Authenticate challenge."""
    verifier = _FakeOAuthVerifier(
        error=OAuthAuthenticationError("invalid_token", "Access token audience/resource does not match Menhir")
    )
    client = _build_oauth_app(verifier)

    resp = client.get("/mcp/test", headers={"Authorization": "Bearer wrong"})

    assert resp.status_code == 401
    assert resp.json()["code"] == "invalid_token"
    assert 'resource_metadata=' in resp.headers["www-authenticate"]


def test_oauth_mcp_insufficient_scope_returns_403():
    """Token with no Menhir scope returns 403 with scope hint in WWW-Authenticate."""
    verifier = _FakeOAuthVerifier(
        error=OAuthAuthenticationError(
            "insufficient_scope",
            "Access token does not include a Menhir scope",
            status_code=403,
            scope="menhir:read menhir:write menhir:admin",
        )
    )
    client = _build_oauth_app(verifier)

    resp = client.get("/mcp/test", headers={"Authorization": "Bearer no-scope-token"})

    assert resp.status_code == 403
    assert resp.json()["code"] == "insufficient_scope"
    assert 'scope="menhir:read menhir:write menhir:admin"' in resp.headers["www-authenticate"]


def test_oauth_api_accepts_valid_token_and_binds_scope_tier_identity():
    """OAuth enabled: /api/* accepts bearer tokens and binds identity from claims."""
    verifier = _FakeOAuthVerifier(
        OAuthPrincipal(
            subject="user-123",
            client_id="chatgpt-client",
            client_name="ChatGPT",
            scopes=frozenset({"menhir:read"}),
            tier="readonly",
        )
    )
    client = _build_oauth_app(verifier)

    resp = client.get("/api/secure")
    assert resp.status_code == 401

    resp = client.get("/api/secure", headers={"Authorization": "Bearer oauth-token"})
    assert resp.status_code == 200
    assert verifier.seen_token == "oauth-token"
    assert resp.json() == {
        "auth_mode": "oauth",
        "client_id": "chatgpt-client",
        "client_name": "ChatGPT",
        "tier": "readonly",
        "user_id": "user-123",
    }


def test_oauth_static_api_key_rejected_for_api_paths_when_enabled():
    """OAuth enabled: static bearer keys no longer authorize protected /api/* routes."""
    verifier = _FakeOAuthVerifier(
        error=OAuthAuthenticationError("invalid_token", "Access token signature or claims are invalid")
    )
    client = _build_oauth_app(verifier, static_api_key="static-key")

    resp = client.get("/api/secure", headers={"Authorization": "Bearer static-key"})
    assert resp.status_code == 401
    assert resp.json()["code"] == "invalid_token"
    assert 'resource_metadata=' in resp.headers["www-authenticate"]
    assert verifier.seen_token == "static-key"


def test_oauth_dev_mode_with_no_static_keys_and_no_oauth():
    """OAuth disabled with no static keys: dev mode allows unprotected access."""
    app = FastAPI()

    @app.get("/api/secure")
    async def secure():
        return JSONResponse({"secure": True})

    wrapped = BearerAuthMiddleware(
        app,
        oauth_config=OAuthConfig(enabled=False),
    )
    client = TestClient(wrapped)

    resp = client.get("/api/secure")
    assert resp.status_code == 200


def test_oauth_token_not_used_as_session_identifier():
    """Raw OAuth token is never used as session_id or client_id."""
    verifier = _FakeOAuthVerifier(
        OAuthPrincipal(
            subject="user-123",
            client_id="chatgpt-client",
            client_name="ChatGPT",
            tier="readonly",
        )
    )
    client = _build_oauth_app(verifier)

    resp = client.get(
        "/mcp/test",
        headers={"Authorization": "Bearer super-secret-oauth-token"},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert "super-secret-oauth-token" not in data["client_id"]
    assert "super-secret-oauth-token" not in data["user_id"]


def test_oauth_no_secrets_in_response():
    """Response body does not contain OAuth token sentinel values."""
    verifier = _FakeOAuthVerifier(
        OAuthPrincipal(
            subject="user-123",
            client_id="chatgpt-client",
            client_name="ChatGPT",
            tier="readonly",
        )
    )
    client = _build_oauth_app(verifier)

    resp = client.get(
        "/mcp/test",
        headers={"Authorization": "Bearer super-secret-oauth-token"},
    )
    body = resp.text
    assert "super-secret-oauth-token" not in body


# ── OAuth identity safety: headers cannot override token claims ───────────────

def test_oauth_x_yawn_user_id_cannot_spoof_subject():
    """OAuth: x-yawn-user-id header does not override principal.subject."""
    verifier = _FakeOAuthVerifier(
        OAuthPrincipal(subject="real-subject", client_id="real-client", tier="readonly")
    )
    client = _build_oauth_app(verifier)

    resp = client.get(
        "/mcp/test",
        headers={
            "Authorization": "Bearer token",
            "x-yawn-user-id": "spoofed-user",
        },
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["user_id"] == "real-subject"
    assert data["user_id"] != "spoofed-user"


def test_oauth_x_yawn_client_id_cannot_spoof_client_id():
    """OAuth: x-yawn-client-id header does not override principal.client_id."""
    verifier = _FakeOAuthVerifier(
        OAuthPrincipal(subject="user", client_id="real-client", tier="readonly")
    )
    client = _build_oauth_app(verifier)

    resp = client.get(
        "/mcp/test",
        headers={
            "Authorization": "Bearer token",
            "x-yawn-client-id": "spoofed-client",
        },
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["client_id"] == "real-client"
    assert data["client_id"] != "spoofed-client"


def test_oauth_uses_principal_subject_as_user_id():
    """OAuth: principal.subject is used as the effective user_id."""
    verifier = _FakeOAuthVerifier(
        OAuthPrincipal(subject="verified-user", tier="readonly")
    )
    client = _build_oauth_app(verifier)

    resp = client.get(
        "/mcp/test",
        headers={"Authorization": "Bearer token"},
    )

    assert resp.status_code == 200
    assert resp.json()["user_id"] == "verified-user"


def test_oauth_uses_principal_client_id_as_client_id():
    """OAuth: principal.client_id is used as the effective client_id."""
    verifier = _FakeOAuthVerifier(
        OAuthPrincipal(subject="user", client_id="verified-client", tier="readonly")
    )
    client = _build_oauth_app(verifier)

    resp = client.get(
        "/mcp/test",
        headers={"Authorization": "Bearer token"},
    )

    assert resp.status_code == 200
    assert resp.json()["client_id"] == "verified-client"


def test_oauth_static_bearer_preserves_x_yawn_headers():
    """Static bearer mode still trusts x-yawn-user-id and x-yawn-client-id headers."""
    app = FastAPI()

    @app.get("/api/secure")
    async def secure():
        session = get_request_session()
        return JSONResponse({
            "user_id": session.user_id if session else "",
            "client_id": session.client_id if session else "",
        })

    wrapped = BearerAuthMiddleware(app, api_key="static-key")
    client = TestClient(wrapped)

    resp = client.get(
        "/api/secure",
        headers={
            "Authorization": "Bearer static-key",
            "x-yawn-user-id": "header-user",
            "x-yawn-client-id": "header-client",
        },
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["user_id"] == "header-user"
    assert data["client_id"] == "header-client"


# ── Identity header rename: x-menhir-* canonical, x-yawn-* deprecated alias ────

def _identity_echo_client(api_key: str = "static-key") -> TestClient:
    app = FastAPI()

    @app.get("/api/secure")
    async def secure():
        session = get_request_session()
        return JSONResponse({
            "user_id": session.user_id if session else "",
            "client_id": session.client_id if session else "",
        })

    return TestClient(BearerAuthMiddleware(app, api_key=api_key))


def test_canonical_menhir_identity_headers_are_read():
    """The x-menhir-* spelling is the canonical identity header set."""
    resp = _identity_echo_client().get(
        "/api/secure",
        headers={
            "Authorization": "Bearer static-key",
            "x-menhir-user-id": "canonical-user",
            "x-menhir-client-id": "canonical-client",
        },
    )

    assert resp.status_code == 200
    assert resp.json() == {"user_id": "canonical-user", "client_id": "canonical-client"}


def test_canonical_header_wins_when_both_spellings_sent():
    """A client sending both must not have the deprecated value take precedence."""
    resp = _identity_echo_client().get(
        "/api/secure",
        headers={
            "Authorization": "Bearer static-key",
            "x-menhir-user-id": "canonical-user",
            "x-yawn-user-id": "legacy-user",
        },
    )

    assert resp.status_code == 200
    assert resp.json()["user_id"] == "canonical-user"


def test_duplicate_authorization_header_is_rejected():
    """Ambiguous credentials must not depend on proxy/client first-vs-last behavior."""
    resp = _identity_echo_client().get(
        "/api/secure",
        headers=[
            ("Authorization", "Bearer static-key"),
            ("Authorization", "Bearer different-key"),
        ],
    )

    assert resp.status_code == 400
    assert resp.json()["code"] == "duplicate_header"


def test_duplicate_identity_header_is_rejected():
    """Caller attribution must have one unambiguous value per spelling."""
    resp = _identity_echo_client().get(
        "/api/secure",
        headers=[
            ("Authorization", "Bearer static-key"),
            ("x-menhir-user-id", "first-user"),
            ("x-menhir-user-id", "second-user"),
        ],
    )

    assert resp.status_code == 400
    assert resp.json()["code"] == "duplicate_header"


def test_deprecated_yawn_alias_cannot_bypass_oauth_identity_gate():
    """The alias must be gated exactly like the canonical header.

    The rename would be a privilege-escalation bug if the legacy spelling were read
    on a path where the canonical one is refused: OAuth mode ignores caller-declared
    identity so verified token claims cannot be overridden, and that must hold for
    both spellings.
    """
    verifier = _FakeOAuthVerifier(
        OAuthPrincipal(subject="real-subject", client_id="real-client", tier="readonly")
    )
    client = _build_oauth_app(verifier)

    resp = client.get(
        "/mcp/test",
        headers={
            "Authorization": "Bearer token",
            "x-yawn-user-id": "spoofed-via-alias",
            "x-menhir-user-id": "spoofed-via-canonical",
        },
    )

    assert resp.status_code == 200
    assert resp.json()["user_id"] == "real-subject"


def _build_oauth_mcp_app(*, static_api_key: str = "") -> TestClient:
    """App with MCP routes for testing query-string API key rejection."""
    verifier = _FakeOAuthVerifier(
        OAuthPrincipal(subject="user", tier="readonly")
    )
    app = FastAPI()

    @app.get("/mcp")
    async def mcp_root():
        return JSONResponse({"ok": True})

    @app.get("/mcp/test")
    async def mcp_sub():
        return JSONResponse({"ok": True})

    @app.get("/mcp-http")
    async def mcp_http():
        return JSONResponse({"ok": True})

    wrapped = BearerAuthMiddleware(
        app,
        api_key=static_api_key,
        oauth_config=_oauth_config(),
        oauth_verifier=verifier,
    )
    return TestClient(wrapped)


def test_oauth_enabled_rejects_api_key_on_mcp():
    """OAuth enabled: ?api_key= is rejected on /mcp, /mcp/*, and /mcp-http."""
    client = _build_oauth_mcp_app(static_api_key="static-key")

    for path in ("/mcp", "/mcp/test", "/mcp-http"):
        resp = client.get(f"{path}?api_key=static-key")

        assert resp.status_code == 401, f"{path} accepted ?api_key="
        assert resp.json()["code"] == "invalid_token", f"{path} expected oauth rejection"


# ── OAuth resource matching ───────────────────────────────────────────────────

def test_oauth_resource_binding_accepts_aud_or_resource_claim():
    accepted = ("https://memory.example.com/mcp-http",)
    assert _resource_matches({"aud": "https://memory.example.com/mcp-http"}, accepted)
    assert _resource_matches({"resource": "https://memory.example.com/mcp-http"}, accepted)
    assert _resource_matches({"aud": ["other", "https://memory.example.com/mcp-http"]}, accepted)
    assert not _resource_matches({"aud": "https://other.example.com"}, accepted)


# ── N-003: IdP/JWKS outage (server_error) -> 503, not a 401 re-auth loop ───────

def test_oauth_server_error_returns_503_without_challenge():
    """A JWKS/IdP outage (server_error) is a service failure, not a credential
    problem: surface 503 with no Bearer challenge so clients retry instead of
    looping on re-authentication."""
    verifier = _FakeOAuthVerifier(
        error=OAuthAuthenticationError("server_error", "Unable to fetch OAuth JWKS")
    )
    client = _build_oauth_app(verifier)

    resp = client.get("/mcp/test", headers={"Authorization": "Bearer tok"})

    assert resp.status_code == 503
    body = resp.json()
    assert body["code"] == "server_error"
    assert body["error"] == "Service Unavailable"
    # RFC 6750 has no server_error challenge code — do not emit one on an outage.
    assert "www-authenticate" not in resp.headers


# ── N-002: CORS preflight (OPTIONS + Origin) is not blocked by auth ────────────

def _build_cors_preflight_client() -> TestClient:
    from starlette.middleware.cors import CORSMiddleware

    app = FastAPI()

    @app.get("/api/secure")
    async def secure():
        return JSONResponse({"secure": True})

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["https://app.example.com"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    return TestClient(BearerAuthMiddleware(app, api_key="secret"))


def test_cors_preflight_passes_through_auth():
    """A browser preflight (OPTIONS + Origin, no Authorization) reaches the inner
    CORSMiddleware instead of being 401'd by the auth wrapper."""
    client = _build_cors_preflight_client()
    resp = client.options(
        "/api/secure",
        headers={
            "Origin": "https://app.example.com",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == "https://app.example.com"


def test_options_without_origin_still_requires_auth():
    """The exemption is preflight-specific: a plain OPTIONS with no Origin is
    still auth-gated."""
    client = _build_cors_preflight_client()
    resp = client.options("/api/secure")
    assert resp.status_code == 401


# ── Explorer route protection ────────────────────────────────────────────────────

def _build_explorer_test_app(
    api_key: str,
    *,
    loopback_bound: bool = True,
    client_host: str = "testclient",
) -> TestClient:
    """Build a test app with explorer routes."""
    app = FastAPI()

    @app.get("/explorer")
    async def explorer_home():
        return JSONResponse({"explorer": "home"})

    @app.post("/explorer/candidates/{uuid}/approve")
    async def approve_candidate(uuid: str):
        return JSONResponse({"status": "approved"})

    @app.get("/explorer/static/explorer.js")
    async def explorer_static():
        return JSONResponse({"static": True})

    @app.get("/api/health")
    async def health():
        return JSONResponse({"ok": True})

    wrapped = BearerAuthMiddleware(app, api_key=api_key, loopback_bound=loopback_bound)
    return TestClient(wrapped, client=(client_host, 50000))


def test_explorer_home_open_on_loopback_even_in_static_mode():
    """GET /explorer is open on a loopback bind, whatever the auth mode.

    The explorer is a local inspection UI opened in a browser -- a browser cannot attach
    a bearer token, and before it was mounted into the app it ran as an unauthenticated
    loopback-only process on :8787. Gating it on loopback would make it unreachable,
    which is a regression, not a hardening. The bind is the boundary: see
    test_explorer_home_requires_bearer_key_on_non_loopback.
    """
    client = _build_explorer_test_app("secret", loopback_bound=True)
    resp = client.get("/explorer")
    assert resp.status_code == 200
    assert resp.json()["explorer"] == "home"


def test_explorer_home_requires_bearer_key_on_non_loopback():
    """On a NON-loopback bind, /explorer is enforced exactly like /api/*.

    This is the boundary that matters: the explorer exposes graph reads AND candidate
    approve/reject writes, so it must never be remotely reachable without a credential.
    """
    client = _build_explorer_test_app("secret", loopback_bound=False)
    resp = client.get("/explorer")
    assert resp.status_code == 401
    assert resp.json()["error"] == "Unauthorized"


def test_explorer_home_open_to_direct_loopback_client_on_network_bind():
    """A local browser remains usable when the same server also accepts LAN API traffic."""
    client = _build_explorer_test_app(
        "secret",
        loopback_bound=False,
        client_host="127.0.0.1",
    )
    resp = client.get("/explorer")
    assert resp.status_code == 200


def test_explorer_loopback_bypass_rejects_forwarded_request():
    """A same-host reverse proxy cannot confer local Explorer access on remote users."""
    client = _build_explorer_test_app(
        "secret",
        loopback_bound=False,
        client_host="127.0.0.1",
    )
    resp = client.get("/explorer", headers={"X-Forwarded-For": "192.0.2.99"})
    assert resp.status_code == 401


def test_explorer_approve_candidate_requires_bearer_key_on_non_loopback():
    """Candidate WRITES must not be remotely reachable unauthenticated."""
    client = _build_explorer_test_app("secret", loopback_bound=False)
    resp = client.post("/explorer/candidates/abc/approve")
    assert resp.status_code == 401


def test_explorer_accepts_bearer_key_on_non_loopback():
    """A valid credential still gets through on a remote bind."""
    client = _build_explorer_test_app("secret", loopback_bound=False)
    resp = client.get("/explorer", headers={"Authorization": "Bearer secret"})
    assert resp.status_code == 200


def test_explorer_home_accepts_valid_bearer_key():
    """GET /explorer accepts bearer token in STATIC mode."""
    client = _build_explorer_test_app("secret")
    resp = client.get("/explorer", headers={"Authorization": "Bearer secret"})
    assert resp.status_code == 200
    assert resp.json()["explorer"] == "home"


def test_explorer_approve_candidate_open_on_loopback():
    """Candidate writes are reachable on loopback (same posture as the old :8787 UI)."""
    client = _build_explorer_test_app("secret", loopback_bound=True)
    resp = client.post("/explorer/candidates/test-uuid/approve")
    assert resp.status_code == 200


def test_explorer_approve_candidate_accepts_valid_bearer_key():
    """POST /explorer/candidates/{uuid}/approve accepts bearer token in STATIC mode."""
    client = _build_explorer_test_app("secret")
    resp = client.post(
        "/explorer/candidates/test-uuid/approve",
        headers={"Authorization": "Bearer secret"}
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "approved"


def test_explorer_static_files_exempt_from_auth():
    """GET /explorer/static/* is exempt from auth (static assets need to load)."""
    client = _build_explorer_test_app("secret")
    resp = client.get("/explorer/static/explorer.js")
    assert resp.status_code == 200
    assert resp.json()["static"] is True


def test_explorer_in_dev_mode_no_auth_required():
    """In dev mode (empty key), /explorer is accessible without a token."""
    client = _build_explorer_test_app("")
    resp = client.get("/explorer")
    assert resp.status_code == 200
    assert resp.json()["explorer"] == "home"
