"""DNS rebinding: a page whose hostname re-resolves to 127.0.0.1 is a loopback peer with no
forwarding header; only its Host names another domain. Credential-free loopback grants must refuse it."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def _scope(path: str, host: bytes | None):
    headers = [(b"host", host)] if host is not None else []
    return {"type": "http", "path": path, "method": "GET", "client": ("127.0.0.1", 51000),
            "headers": headers, "query_string": b""}


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


@pytest.mark.asyncio
@pytest.mark.parametrize("path,kw", [
    ("/explorer", {"operator_key": "secret", "loopback_bound": True}),  # explorer pass-through
    ("/api/anything", {}),  # AuthMode.NONE grants operator tier
])
async def test_rebound_host_is_refused_and_local_hosts_still_pass(path, kw):
    assert not await _reached(_mw(**kw), _scope(path, b"evil.example:8000"))
    assert not await _reached(_mw(**kw), _scope(path, b"127.0.0.1.evil.example"))
    for ok in (b"127.0.0.1:8000", b"localhost", b"[::1]:8000", None):
        assert await _reached(_mw(**kw), _scope(path, ok)), ok
