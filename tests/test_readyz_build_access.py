"""Who may see /readyz's build block: only a caller the MCP endpoint would also accept.

The public body must stay what a health check needs. The exact build and the digest (which shows
whether snapshot upload is on) go only to a caller with a valid token, and a bad token must never
change the readiness answer.
"""

from __future__ import annotations

import http.server
import importlib.util
import json
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

from menhir.api.oauth import OAuthAuthenticationError
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
    return SimpleNamespace(wrapped=wrapped, inner=inner, client=TestClient(wrapped))


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
    assert "no-store" not in response.headers.get("cache-control", "")


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
    return SimpleNamespace(client_id="c", scopes=("menhir:read",), tier="readonly")


def _request(*headers: tuple[bytes, bytes], verifier: Any, policy: Any) -> SimpleNamespace:
    state = SimpleNamespace(oauth_verifier=verifier, client_policy=policy)
    return SimpleNamespace(headers=Headers(raw=list(headers)), app=SimpleNamespace(state=state))


_BEARER = (b"authorization", b"Bearer t")


def _verifier(result: Any = None, *, raises: Exception | None = None) -> SimpleNamespace:
    mock = AsyncMock(return_value=result, side_effect=raises)
    return SimpleNamespace(verify_access_token=mock)


async def test_the_check_passes_the_token_without_its_scheme_and_checks_the_policy() -> None:
    verifier = _verifier(_principal())
    policy = SimpleNamespace(require_client=lambda **kwargs: kwargs)
    assert await _caller_may_see_build(_request(_BEARER, verifier=verifier, policy=policy))
    verifier.verify_access_token.assert_awaited_once_with("t")


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


@pytest.mark.parametrize(
    ("verifier", "policy"),
    [(None, object()), (object(), None), (None, None)],
    ids=["no-verifier", "no-policy", "neither"],
)
async def test_missing_wiring_is_no(verifier: Any, policy: Any) -> None:
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
    response = await readyz(SimpleNamespace(app=SimpleNamespace(state=state)))
    assert set(json.loads(response.body)) == PUBLIC_KEYS
    assert calls == []


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
    monkeypatch.setenv(check_script.TOKEN_ENV, "secret-token")
    assert check_script.main(["http://menhir.example.com"]) == check_script.EXIT_ERROR
    out = capsys.readouterr().out
    assert "HTTPS" in out and "secret-token" not in out


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
