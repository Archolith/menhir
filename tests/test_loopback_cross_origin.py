"""Cross-site requests: a page on any site can send a body-less POST to http://localhost:<port>/
from the user's browser. Host is localhost, so the DNS-rebinding check passes; only Origin (or
Sec-Fetch-Site) shows the request came from another site. Credential-free loopback grants must
refuse it, and non-browser callers, which send neither header, must still pass."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def _scope(path: str, *, method: str = "POST", origin: bytes | None = None,
           sec_fetch_site: bytes | None = None, scope_type: str = "http"):
    headers = [(b"host", b"localhost:8100")]
    if origin is not None:
        headers.append((b"origin", origin))
    if sec_fetch_site is not None:
        headers.append((b"sec-fetch-site", sec_fetch_site))
    return {"type": scope_type, "path": path, "method": method,
            "client": ("127.0.0.1", 51000), "headers": headers, "query_string": b""}


async def _reached(mw, scope) -> bool:
    async def receive():  # pragma: no cover
        return {}

    async def send(message):
        pass

    await mw(scope, receive, send)
    return bool(scope.get("reached_app"))


def _mw(**kw):
    from menhir.api.auth import BearerAuthMiddleware

    async def app(scope, receive, send):
        scope["reached_app"] = True

    return BearerAuthMiddleware(app, **kw)


# The credential-free loopback grants that hand out authority without a credential.
_GRANTS = [
    pytest.param("/api/phase3/reset", {}, id="no-auth-operator"),
    pytest.param("/explorer/api/candidates/x/approve",
                 {"operator_key": "secret", "loopback_bound": True}, id="explorer"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("path,kw", _GRANTS)
@pytest.mark.parametrize("origin", [
    b"https://evil.example",
    b"http://evil.example:8100",
    b"null",
    b"http://localhost.evil.example",
    b"http://evil.localhost",
    b"http://127.0.0.1.evil.example",
    b"file://",
])
async def test_foreign_origin_is_refused(path, kw, origin):
    assert not await _reached(_mw(**kw), _scope(path, origin=origin))


@pytest.mark.asyncio
@pytest.mark.parametrize("path,kw", _GRANTS)
@pytest.mark.parametrize("origin", [
    None,  # stdio bridge, CLI, BackendClient, curl
    b"http://localhost:8100",
    b"http://localhost:6274",  # a local browser tool on another port
    b"http://127.0.0.1:3000",
    b"http://[::1]:8100",
    b"HTTP://LOCALHOST:8100/",
])
async def test_local_or_absent_origin_still_passes(path, kw, origin):
    assert await _reached(_mw(**kw), _scope(path, origin=origin))


@pytest.mark.asyncio
async def test_configured_cors_origin_is_accepted():
    mw = _mw(allowed_origins=("https://app.example.com/",))
    assert await _reached(mw, _scope("/api/x", origin=b"https://APP.example.com"))
    assert not await _reached(mw, _scope("/api/x", origin=b"https://other.example.com"))


@pytest.mark.asyncio
async def test_sec_fetch_site_cross_site_refuses_writes_but_not_navigation():
    # No Origin header (older browsers): Sec-Fetch-Site still marks the request.
    assert not await _reached(_mw(), _scope("/api/x", sec_fetch_site=b"cross-site"))
    assert not await _reached(
        _mw(), _scope("/api/x", method="GET", sec_fetch_site=b"cross-site", scope_type="websocket")
    )
    # A link click to the explorer is a cross-site GET navigation and must keep working.
    assert await _reached(
        _mw(operator_key="secret", loopback_bound=True),
        _scope("/explorer", method="GET", sec_fetch_site=b"cross-site"),
    )
    assert await _reached(_mw(), _scope("/api/x", sec_fetch_site=b"same-origin"))


@pytest.mark.asyncio
async def test_cross_site_page_cannot_bootstrap_mint_a_client_token(tmp_path):
    from menhir.api.client_token_store import ClientTokenStore

    store = ClientTokenStore(tmp_path / "client_tokens.db")
    mw = _mw(client_token_store=store, loopback_bound=True)
    assert not await _reached(mw, _scope("/api/admin/clients", origin=b"https://evil.example"))
    assert await _reached(mw, _scope("/api/admin/clients"))
