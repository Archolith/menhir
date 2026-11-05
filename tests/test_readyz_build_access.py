"""Who may see /readyz's build block: only a caller the MCP endpoint would also accept.

The public body must stay what a health check needs. The exact build and the digest (which shows
whether snapshot upload is on) go only to a caller with a valid token, and a bad token must never
change the readiness answer.
"""

from __future__ import annotations

import asyncio
import http.server
import importlib.util
import json
import logging
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from joserfc import jwt as jose_jwt
from joserfc.jwk import KeySet, RSAKey
from starlette.datastructures import Headers

from menhir.api import production_routes
from menhir.api.oauth import JwksRefreshStats, OAuthAuthenticationError
from menhir.api.production_routes import _caller_may_see_build, readyz
from menhir.api.server import create_app
from menhir.mcp import served_metadata
from tests.test_production_runtime_surface import _production_settings

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "release" / "check_served_metadata.py"
_spec = importlib.util.spec_from_file_location("check_served_metadata_access", SCRIPT)
check_script = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_script)

PUBLIC_KEYS = {"status", "mode", "mutation_fence", "scheduler", "failures", "oauth_jwks"}
_KID = "readyz-test-key"
_ISSUER = "https://menhir.example.com"
_RESOURCE = "https://menhir.example.com/mcp-http"


@pytest.fixture(autouse=True)
def _fresh_digest_cache():
    served_metadata._reset_digest_cache()
    yield
    served_metadata._reset_digest_cache()


@pytest.fixture(autouse=True)
def _no_ambient_proxy(monkeypatch):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)
    monkeypatch.setenv("NO_PROXY", "*")


# --- the real stack: real middleware, real verifier, real client policy ---------------------


@pytest.fixture(scope="module")
def signing_key() -> Any:
    return RSAKey.generate_key(2048, parameters={"kid": _KID}, private=True)


@pytest.fixture
def stack(tmp_path: Path, signing_key: Any) -> SimpleNamespace:
    wrapped = create_app(
        settings=_production_settings(
            oauth_as_dir=str(tmp_path / "oauth"),
            oauth_signing_key_path=str(tmp_path / "oauth-signing-key.test.json"),
        )
    )
    inner = wrapped.app.app
    verifier = inner.state.oauth_verifier
    verifier._algorithms = ["RS256"]
    verifier._jwks = KeySet.import_key_set({"keys": [signing_key.as_dict(private=False)]})
    verifier._jwks_expires_at = time.monotonic() + 3600.0
    verifier._load_jwks = AsyncMock(return_value=verifier._jwks)
    return SimpleNamespace(
        wrapped=wrapped,
        inner=inner,
        # No lifespan here, so a request that gets past auth on /mcp-http answers 500.
        client=TestClient(wrapped, raise_server_exceptions=False),
    )


def _policy_client() -> tuple[str, list[str]]:
    """A client in the synthetic policy, with the exact scopes the policy requires of it."""

    policy_path = ROOT / "tests" / "fixtures" / "client-policy.synthetic.json"
    clients = json.loads(policy_path.read_text(encoding="utf-8"))["clients"]
    for client_id, client in clients.items():
        if client["scopes"] == ["menhir:read"]:
            return client_id, client["scopes"]
    raise AssertionError("the synthetic policy has no read-only client")


def _token(key: Any, **overrides: Any) -> str:
    client_id, scopes = _policy_client()
    claims: dict[str, Any] = {
        "iss": _ISSUER,
        "aud": _RESOURCE,
        "sub": "someone",
        "exp": int(time.time()) + 300,
        "client_id": client_id,
        "scope": " ".join(scopes),
    }
    claims.update(overrides)
    return jose_jwt.encode({"alg": "RS256", "kid": _KID}, claims, key)


def _get(stack: SimpleNamespace, token: str | None = None) -> Any:
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    return stack.client.get("/readyz", headers=headers)


def test_public_body_has_no_build_details(stack: SimpleNamespace) -> None:
    response = _get(stack)
    assert set(response.json()) == PUBLIC_KEYS
    assert "commit" not in response.text and "sha256" not in response.text
    # A cached public body would make the check script report a good token as refused.
    assert response.headers["cache-control"] == "no-store"


def test_in_the_ready_state_only_a_valid_token_changes_the_body(
    stack: SimpleNamespace, signing_key: Any
) -> None:
    stack.inner.state.runtime_ctx = SimpleNamespace(
        capabilities=SimpleNamespace(neo4j_ready=True, enrichment_ready=True),
        scheduler=object(),
    )
    stack.inner.state.oauth_signing_key = object()
    public = _get(stack)
    valid = _get(stack, _token(signing_key))
    refused = _get(stack, _token(signing_key, exp=1000))
    for response in (public, valid, refused):
        assert response.status_code == 200
        assert response.json()["status"] == "ready"
        assert response.json()["failures"] == []
    assert "build" in valid.json()
    assert "build" not in public.json() and "build" not in refused.json()


def test_the_mcp_endpoint_lets_the_valid_token_past_auth(
    stack: SimpleNamespace, signing_key: Any
) -> None:
    """The control for the refusal test below: there, 401/403 means the token was the problem."""
    response = stack.client.post(
        "/mcp-http", headers={"Authorization": f"Bearer {_token(signing_key)}"}, json={}
    )
    assert response.status_code == 500


def test_a_valid_token_sees_the_build_and_the_response_is_not_cached(
    stack: SimpleNamespace, signing_key: Any
) -> None:
    response = _get(stack, _token(signing_key))
    assert set(response.json()) == PUBLIC_KEYS | {"build"}
    assert set(response.json()["build"]) == {"version", "commit", "mcp_metadata_sha256"}
    assert response.headers["cache-control"] == "no-store"


def test_a_token_does_not_change_the_readiness_answer(
    stack: SimpleNamespace, signing_key: Any
) -> None:
    public = _get(stack)
    withtoken = _get(stack, _token(signing_key))
    assert withtoken.status_code == public.status_code
    assert withtoken.json()["status"] == public.json()["status"]
    assert withtoken.json()["failures"] == public.json()["failures"]


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"exp": 1000}, id="expired"),
        pytest.param({"iss": "https://elsewhere.example.com"}, id="wrong-issuer"),
        pytest.param({"aud": "https://elsewhere.example.com/mcp"}, id="wrong-audience"),
        pytest.param({"client_id": "not-in-the-policy"}, id="client-not-in-policy"),
        pytest.param({"scope": "menhir:read menhir:write"}, id="scopes-differ-from-policy"),
        pytest.param({"scope": "unrelated"}, id="no-menhir-scope"),
    ],
)
def test_a_token_the_mcp_endpoint_would_refuse_sees_no_build(
    stack: SimpleNamespace, signing_key: Any, overrides: dict[str, Any]
) -> None:
    public = _get(stack)
    token = _token(signing_key, **overrides)
    response = _get(stack, token)
    assert set(response.json()) == PUBLIC_KEYS
    assert response.status_code == public.status_code
    # The boundary this mirrors: the protected endpoint refuses the same token.
    assert stack.client.post(
        "/mcp-http", headers={"Authorization": f"Bearer {token}"}, json={}
    ).status_code in {401, 403}


def test_a_token_from_another_key_sees_no_build(stack: SimpleNamespace) -> None:
    other = RSAKey.generate_key(2048, parameters={"kid": _KID}, private=True)
    assert set(_get(stack, _token(other)).json()) == PUBLIC_KEYS


def test_garbage_and_empty_bearer_values_see_no_build(stack: SimpleNamespace) -> None:
    assert set(_get(stack, "not-a-jwt").json()) == PUBLIC_KEYS
    response = stack.client.get("/readyz", headers={"Authorization": "Bearer "})
    assert set(response.json()) == PUBLIC_KEYS


def test_two_authorization_headers_see_no_build(stack: SimpleNamespace, signing_key: Any) -> None:
    good = f"Bearer {_token(signing_key)}"
    response = stack.client.get(
        "/readyz", headers=[("Authorization", good), ("Authorization", good)]
    )
    assert set(response.json()) == PUBLIC_KEYS


@pytest.mark.parametrize("name", ["x-menhir-client-id", "x-menhir-namespace"])
def test_duplicate_identity_headers_see_no_build(
    stack: SimpleNamespace, signing_key: Any, name: str
) -> None:
    headers = [("Authorization", f"Bearer {_token(signing_key)}"), (name, "a"), (name, "b")]
    assert set(stack.client.get("/readyz", headers=headers).json()) == PUBLIC_KEYS
    assert stack.client.post("/mcp-http", headers=headers, json={}).status_code == 400


def test_a_token_in_the_query_string_sees_no_build(
    stack: SimpleNamespace, signing_key: Any
) -> None:
    response = stack.client.get("/readyz", params={"api_key": _token(signing_key)})
    assert set(response.json()) == PUBLIC_KEYS


def test_the_static_api_key_sees_no_build(stack: SimpleNamespace) -> None:
    """Production settings still carry api_key, but the MCP endpoint is OAuth-only there."""
    assert set(_get(stack, "test-api-key").json()) == PUBLIC_KEYS


def test_the_middleware_and_the_route_share_one_verifier(stack: SimpleNamespace) -> None:
    assert stack.wrapped.app._oauth_verifier is stack.inner.state.oauth_verifier


# --- the check itself: every doubt is "no" ----------------------------------------------------


def _principal() -> SimpleNamespace:
    return SimpleNamespace(client_id="c", scopes=("menhir:write",), tier="agent")


def _request(*headers: tuple[bytes, bytes], verifier: Any, policy: Any) -> SimpleNamespace:
    state = SimpleNamespace(oauth_verifier=verifier, client_policy=policy)
    return SimpleNamespace(headers=Headers(raw=list(headers)), app=SimpleNamespace(state=state))


_BEARER = (b"authorization", b"Bearer t")


def _verifier(result: Any = None, *, raises: Exception | None = None) -> SimpleNamespace:
    mock = AsyncMock(return_value=result, side_effect=raises)
    return SimpleNamespace(verify_access_token=mock)


async def test_the_check_passes_the_token_without_its_scheme_and_checks_the_policy() -> None:
    verifier = _verifier(_principal())
    checked: list[dict[str, Any]] = []
    policy = SimpleNamespace(require_client=lambda **kwargs: checked.append(kwargs))
    assert await _caller_may_see_build(_request(_BEARER, verifier=verifier, policy=policy))
    verifier.verify_access_token.assert_awaited_once_with("t")
    assert checked == [{"client_id": "c", "scopes": ("menhir:write",), "tier": "agent"}]


@pytest.mark.parametrize(
    "exc",
    [
        OAuthAuthenticationError("invalid_token", "bad"),
        OAuthAuthenticationError("server_error", "jwks down"),
        RuntimeError("boom"),
    ],
    ids=["invalid-token", "jwks-outage", "unexpected"],
)
async def test_a_verifier_failure_is_no(exc: Exception) -> None:
    request = _request(
        _BEARER, verifier=_verifier(raises=exc), policy=SimpleNamespace(require_client=print)
    )
    assert await _caller_may_see_build(request) is False


async def test_a_policy_refusal_is_no() -> None:
    def refuse(**_kwargs: Any) -> None:
        raise PermissionError("not this client")

    request = _request(
        _BEARER, verifier=_verifier(_principal()), policy=SimpleNamespace(require_client=refuse)
    )
    assert await _caller_may_see_build(request) is False


async def test_cancellation_still_propagates() -> None:
    request = _request(
        _BEARER,
        verifier=_verifier(raises=asyncio.CancelledError()),
        policy=SimpleNamespace(require_client=print),
    )
    with pytest.raises(asyncio.CancelledError):
        await _caller_may_see_build(request)


@pytest.mark.parametrize(
    ("verifier", "refuse", "reason"),
    [
        (_verifier(raises=OAuthAuthenticationError("invalid_token", "tok-text")), False, "invalid_token"),
        (_verifier(_principal()), True, "client_policy"),
    ],
    ids=["bad-token", "policy"],
)
async def test_a_refusal_logs_a_bounded_reason_below_warning(
    caplog, verifier: Any, refuse: bool, reason: str
) -> None:
    def require_client(**_kwargs: Any) -> None:
        if refuse:
            raise PermissionError("tok-text")

    request = _request(_BEARER, verifier=verifier, policy=SimpleNamespace(require_client=require_client))
    with caplog.at_level(logging.INFO, logger=production_routes.__name__):
        assert await _caller_may_see_build(request) is False
    records = [r for r in caplog.records if r.name == production_routes.__name__]
    assert [r.levelno for r in records] == [logging.INFO]
    assert reason in records[0].getMessage()
    assert "tok-text" not in caplog.text


async def test_a_server_side_failure_logs_a_warning(caplog) -> None:
    request = _request(
        _BEARER,
        verifier=_verifier(raises=OAuthAuthenticationError("server_error", "tok-text")),
        policy=SimpleNamespace(require_client=print),
    )
    with caplog.at_level(logging.INFO, logger=production_routes.__name__):
        assert await _caller_may_see_build(request) is False
    records = [r for r in caplog.records if r.name == production_routes.__name__]
    assert [r.levelno for r in records] == [logging.WARNING]
    assert "tok-text" not in caplog.text


async def test_a_duplicate_header_refusal_is_logged(caplog) -> None:
    request = _request(
        _BEARER,
        (b"x-menhir-namespace", b"a"),
        (b"x-menhir-namespace", b"b"),
        verifier=_verifier(_principal()),
        policy=SimpleNamespace(require_client=print),
    )
    with caplog.at_level(logging.INFO, logger=production_routes.__name__):
        assert await _caller_may_see_build(request) is False
    assert "duplicate_header" in caplog.text


async def test_missing_wiring_is_logged(caplog) -> None:
    request = _request(_BEARER, verifier=None, policy=None)
    with caplog.at_level(logging.INFO, logger=production_routes.__name__):
        assert await _caller_may_see_build(request) is False
    assert "not_configured" in caplog.text


@pytest.mark.parametrize(
    ("has_verifier", "has_policy"),
    [(False, True), (True, False), (False, False)],
    ids=["no-verifier", "no-policy", "neither"],
)
async def test_missing_wiring_is_no(has_verifier: bool, has_policy: bool) -> None:
    verifier = _verifier(_principal()) if has_verifier else None
    policy = SimpleNamespace(require_client=lambda **_kwargs: None) if has_policy else None
    assert await _caller_may_see_build(_request(_BEARER, verifier=verifier, policy=policy)) is False


@pytest.mark.parametrize(
    "value",
    [b"Basic dGVzdA==", b"bearer t", b"Bearer", b"t"],
    ids=["basic", "lowercase-scheme", "no-token", "no-scheme"],
)
async def test_anything_but_a_bearer_header_never_reaches_the_verifier(value: bytes) -> None:
    verifier = _verifier(_principal())
    request = _request(
        (b"authorization", value), verifier=verifier, policy=SimpleNamespace(require_client=print)
    )
    assert await _caller_may_see_build(request) is False
    verifier.verify_access_token.assert_not_awaited()


async def test_a_public_call_does_not_compute_the_digest(monkeypatch) -> None:
    calls: list[object] = []

    async def counting(*args: object) -> str:
        calls.append(args)
        return "0" * 64

    monkeypatch.setattr(served_metadata, "_compute_digest", counting)
    state = SimpleNamespace(
        settings=SimpleNamespace(runtime_mode="production", instance_id="x"),
        runtime_ctx=SimpleNamespace(
            capabilities=SimpleNamespace(neo4j_ready=True, enrichment_ready=True),
            scheduler=object(),
        ),
        oauth_signing_key=object(),
    )
    response = await readyz(SimpleNamespace(headers=Headers(), app=SimpleNamespace(state=state)))
    assert set(json.loads(response.body)) == PUBLIC_KEYS
    assert calls == []


async def test_the_readiness_answer_is_read_after_the_token_check(monkeypatch) -> None:
    """The token check can wait on the JWKS lock; the answer must not be from before that wait."""
    capabilities = SimpleNamespace(neo4j_ready=True, enrichment_ready=True)

    async def gate(_request: object) -> bool:
        capabilities.neo4j_ready = False
        return False

    monkeypatch.setattr(production_routes, "_caller_may_see_build", gate)
    state = SimpleNamespace(
        settings=SimpleNamespace(runtime_mode="production", instance_id="x"),
        runtime_ctx=SimpleNamespace(capabilities=capabilities, scheduler=object()),
        oauth_signing_key=object(),
    )
    response = await readyz(SimpleNamespace(headers=Headers(), app=SimpleNamespace(state=state)))
    assert response.status_code == 503
    assert json.loads(response.body)["failures"] == ["neo4j_unavailable"]


async def test_the_readiness_answer_is_read_after_the_digest_wait(monkeypatch) -> None:
    capabilities = SimpleNamespace(neo4j_ready=True, enrichment_ready=True)

    async def gate(_request: object) -> bool:
        return True

    async def digest(_instance: object) -> str:
        capabilities.neo4j_ready = False
        return "0" * 64

    monkeypatch.setattr(production_routes, "_caller_may_see_build", gate)
    monkeypatch.setattr(production_routes, "served_mcp_metadata_digest", digest)
    state = SimpleNamespace(
        settings=SimpleNamespace(runtime_mode="production", instance_id="x"),
        runtime_ctx=SimpleNamespace(capabilities=capabilities, scheduler=object()),
        oauth_signing_key=object(),
    )
    response = await readyz(SimpleNamespace(headers=Headers(), app=SimpleNamespace(state=state)))
    assert response.status_code == 503
    assert "build" in json.loads(response.body)


async def test_a_jwks_failure_during_the_token_check_is_in_the_same_response(monkeypatch) -> None:
    """The check script names this failure, so it must not wait for the next request."""
    stats = JwksRefreshStats()

    async def gate(_request: object) -> bool:
        stats.record("ConnectTimeout")
        return False

    monkeypatch.setattr(production_routes, "JWKS_REFRESH_STATS", stats)
    monkeypatch.setattr(production_routes, "_caller_may_see_build", gate)
    state = SimpleNamespace(
        settings=SimpleNamespace(runtime_mode="production", instance_id="x"),
        runtime_ctx=None,
        oauth_signing_key=object(),
    )
    response = await readyz(SimpleNamespace(headers=Headers(), app=SimpleNamespace(state=state)))
    assert json.loads(response.body)["oauth_jwks"]["last_failure_kind"] == "ConnectTimeout"


# --- the check script ------------------------------------------------------------------------


class _Recorder(http.server.BaseHTTPRequestHandler):
    status = 200
    body = b"{}"
    location = ""
    seen: list[str | None]

    def do_GET(self) -> None:  # noqa: N802
        self.seen.append(self.headers.get("Authorization"))
        self.send_response(self.status)
        if self.location:
            self.send_header("Location", self.location)
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def serve():
    servers: list[http.server.HTTPServer] = []

    def start(**attrs: Any) -> tuple[str, list[str | None]]:
        seen: list[str | None] = []
        handler = type("Handler", (_Recorder,), {"seen": seen, **attrs})
        server = http.server.HTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_address[1]}", seen

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def test_the_script_sends_the_env_token(serve, monkeypatch) -> None:
    monkeypatch.setenv(check_script.TOKEN_ENV, "secret-token")
    url, seen = serve()
    check_script.main([url])
    assert seen == ["Bearer secret-token"]


@pytest.mark.parametrize("value", [None, "", "   "], ids=["unset", "empty", "blank"])
def test_the_script_sends_no_header_without_a_token(serve, monkeypatch, value) -> None:
    if value is None:
        monkeypatch.delenv(check_script.TOKEN_ENV, raising=False)
    else:
        monkeypatch.setenv(check_script.TOKEN_ENV, value)
    url, seen = serve()
    check_script.main([url])
    assert seen == [None]


def test_no_build_block_without_a_token_names_the_env_var(serve, monkeypatch, capsys) -> None:
    monkeypatch.delenv(check_script.TOKEN_ENV, raising=False)
    url, _ = serve()
    assert check_script.main([url]) == check_script.EXIT_ERROR
    assert check_script.TOKEN_ENV in capsys.readouterr().out


def test_no_build_block_with_a_token_says_it_was_not_accepted(serve, monkeypatch, capsys) -> None:
    monkeypatch.setenv(check_script.TOKEN_ENV, "secret-token")
    url, _ = serve()
    assert check_script.main([url]) == check_script.EXIT_ERROR
    out = capsys.readouterr().out
    assert "did not accept the token" in out and "secret-token" not in out


def test_the_script_refuses_to_send_a_token_over_plain_http(monkeypatch, capsys) -> None:
    opened: list[object] = []
    monkeypatch.setattr(check_script.urllib.request, "build_opener", lambda *a: opened.append(a))
    monkeypatch.setenv(check_script.TOKEN_ENV, "secret-token")
    assert check_script.main(["http://menhir.example.com"]) == check_script.EXIT_ERROR
    out = capsys.readouterr().out
    assert "HTTPS" in out and "secret-token" not in out
    assert opened == []


@pytest.mark.parametrize(
    "url",
    [
        "https://menhir.example.com",
        "HTTPS://menhir.example.com:8443/base",
        "http://127.0.0.1:8000",
        "http://localhost",
        "HTTP://LOCALHOST:8000",
        "http://[::1]:8000",
    ],
)
def test_token_urls_allowed(url: str) -> None:
    check_script._require_safe_token_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://menhir.example.com",
        "http://localhost.evil.example",
        "http://127.0.0.1.evil.example",
        "http://127.0.0.1@evil.example",
        "http://evil.example@127.0.0.1:8000",
        "http://evil.example%2f@127.0.0.1",
        "http://evil.example\\@127.0.0.1",
        "http://localhost:evil.example:80",
        "https://someone@menhir.example.com",
        "https://menhir.example.com:notaport",
        "ftp://localhost:2121",
        "file://localhost/x",
        "ftp://menhir.example.com",
        "localhost",
    ],
)
def test_token_urls_refused(url: str) -> None:
    with pytest.raises(check_script.CheckError, match="HTTPS"):
        check_script._require_safe_token_url(url)


def test_a_loopback_token_never_goes_to_a_proxy(serve, monkeypatch) -> None:
    proxy, proxy_seen = serve()
    target, target_seen = serve()
    monkeypatch.delenv("NO_PROXY")
    monkeypatch.setenv("HTTP_PROXY", proxy)
    monkeypatch.delenv(check_script.TOKEN_ENV, raising=False)
    check_script.main([target])
    assert (proxy_seen, target_seen) == ([None], [])  # control: this proxy is really in use

    monkeypatch.setenv(check_script.TOKEN_ENV, "secret-token")
    check_script.main([target])
    assert proxy_seen == [None]
    assert target_seen == ["Bearer secret-token"]


def test_the_https_token_opener_keeps_the_proxy_but_never_follows_redirects(monkeypatch) -> None:
    monkeypatch.delenv("NO_PROXY")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    handlers = check_script._token_opener("https://menhir.example.com").handlers
    assert any(isinstance(h, check_script._NoRedirect) for h in handlers)
    assert not any(type(h) is check_script.urllib.request.HTTPRedirectHandler for h in handlers)
    # Only a CONNECT reaches the proxy over HTTPS, so the token stays inside TLS.
    assert any(
        isinstance(h, check_script.urllib.request.ProxyHandler) and "https" in h.proxies
        for h in handlers
    )


_MATCHING_BUILD = json.dumps(
    {"build": {"version": "1", "commit": "unknown", "mcp_metadata_sha256": "0" * 64}}
).encode()


def _set_token(monkeypatch, with_token: bool) -> None:
    if with_token:
        monkeypatch.setenv(check_script.TOKEN_ENV, "secret-token")
    else:
        monkeypatch.delenv(check_script.TOKEN_ENV, raising=False)


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_a_redirect_answer_is_an_error_even_with_a_matching_body(
    serve, monkeypatch, capsys, status: int
) -> None:
    monkeypatch.setenv(check_script.TOKEN_ENV, "secret-token")
    url, _ = serve(status=status, body=_MATCHING_BUILD)
    assert check_script.main([url, "--expect-version", "1"]) == check_script.EXIT_ERROR
    out = capsys.readouterr().out
    assert f"HTTP {status}" in out and "redirect" in out


@pytest.mark.parametrize("with_token", [True, False], ids=["token", "no-token"])
@pytest.mark.parametrize("status", [201, 202, 204, 206, 300, 304, 305, 500])
def test_any_other_status_is_named_and_its_body_ignored(
    serve, monkeypatch, capsys, status: int, with_token: bool
) -> None:
    _set_token(monkeypatch, with_token)
    url, _ = serve(status=status, body=_MATCHING_BUILD)
    assert check_script.main([url, "--expect-version", "1"]) == check_script.EXIT_ERROR
    out = capsys.readouterr().out
    assert f"HTTP {status}" in out and "redirect" not in out


@pytest.mark.parametrize("with_token", [True, False], ids=["token", "no-token"])
def test_a_404_points_at_the_surface_or_the_url(serve, monkeypatch, capsys, with_token) -> None:
    _set_token(monkeypatch, with_token)
    url, _ = serve(status=404, body=b'{"detail":"Not Found"}')
    assert check_script.main([url]) == check_script.EXIT_ERROR
    out = capsys.readouterr().out
    assert "HTTP 404" in out and "production surface" in out


def test_a_503_body_is_still_read(serve, monkeypatch, capsys) -> None:
    monkeypatch.setenv(check_script.TOKEN_ENV, "secret-token")
    url, _ = serve(status=503, body=b"{}")
    assert check_script.main([url]) == check_script.EXIT_ERROR
    assert "did not accept the token" in capsys.readouterr().out


def test_a_jwks_failure_is_named_when_the_token_was_not_accepted(
    serve, monkeypatch, capsys
) -> None:
    monkeypatch.setenv(check_script.TOKEN_ENV, "secret-token")
    body = {"oauth_jwks": {"last_failure_kind": "ConnectTimeout", "last_failure_at": "2026-11-04"}}
    url, _ = serve(body=json.dumps(body).encode())
    assert check_script.main([url]) == check_script.EXIT_ERROR
    out = capsys.readouterr().out
    assert "ConnectTimeout" in out and "2026-11-04" in out


@pytest.mark.parametrize(
    ("kind", "at"),
    [
        ("X\nversion: ok\n::error::forged", "2026-11-04"),
        ("☃", "2026-11-04"),
        ("\ud800", "2026-11-04"),
        ("a" * 200, "2026-11-04"),
        (5, "2026-11-04"),
        (["a"], "2026-11-04"),
        ("ConnectTimeout", "2026\nforged"),
        ("ConnectTimeout", None),
    ],
    ids=["newlines", "non-ascii", "surrogate", "long", "number", "list", "bad-time", "no-time"],
)
def test_server_text_that_is_not_plain_is_not_printed(
    serve, monkeypatch, capsys, kind: Any, at: Any
) -> None:
    monkeypatch.setenv(check_script.TOKEN_ENV, "secret-token")
    body = {"oauth_jwks": {"last_failure_kind": kind, "last_failure_at": at}}
    url, _ = serve(body=json.dumps(body).encode())
    assert check_script.main([url]) == check_script.EXIT_ERROR
    out = capsys.readouterr().out
    assert "reports a JWKS refresh failure" in out
    assert "forged" not in out and "last JWKS refresh failure was" not in out


@pytest.mark.parametrize("jwks", [None, [], "x", 5, {}], ids=["none", "list", "text", "number", "empty"])
def test_an_unusable_jwks_field_adds_no_note(serve, monkeypatch, capsys, jwks: Any) -> None:
    monkeypatch.setenv(check_script.TOKEN_ENV, "secret-token")
    url, _ = serve(body=json.dumps({"oauth_jwks": jwks}).encode())
    assert check_script.main([url]) == check_script.EXIT_ERROR
    out = capsys.readouterr().out
    assert "did not accept the token" in out and "JWKS" not in out


@pytest.mark.parametrize(
    "token",
    ["secret-token", "secret-token\nmore", "secret-token\r\nX-Other: y"],
    ids=["refused-connection", "newline", "header-injection"],
)
def test_a_failed_fetch_does_not_print_the_token(monkeypatch, capsys, token: str) -> None:
    monkeypatch.setenv(check_script.TOKEN_ENV, token)
    assert check_script.main(["http://127.0.0.1:9", "--timeout", "1"]) == check_script.EXIT_ERROR
    captured = capsys.readouterr()
    assert "secret-token" not in captured.out + captured.err


def test_help_names_the_token_variable(capsys) -> None:
    with pytest.raises(SystemExit):
        check_script.main(["--help"])
    assert check_script.TOKEN_ENV in capsys.readouterr().out


def test_a_redirect_never_carries_the_token_elsewhere(serve, monkeypatch, capsys) -> None:
    monkeypatch.setenv(check_script.TOKEN_ENV, "secret-token")
    other, other_seen = serve()
    url, seen = serve(status=302, location=f"{other}/readyz")
    assert check_script.main([url]) == check_script.EXIT_ERROR
    assert seen == ["Bearer secret-token"]
    assert other_seen == []
    assert "secret-token" not in capsys.readouterr().out


def test_without_a_token_a_redirect_is_still_followed(serve, monkeypatch) -> None:
    monkeypatch.delenv(check_script.TOKEN_ENV, raising=False)
    other, other_seen = serve()
    url, _ = serve(status=302, location=f"{other}/readyz")
    check_script.main([url])
    assert other_seen == [None]
