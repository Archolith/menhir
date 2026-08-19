"""Counterexample tests for CF-8: the explorer's loopback exemption ignored proxy forwarding.

Reproduces the scenario the register recorded, not the shape of the fix.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

_FORWARDING_HEADERS = [
    (b"x-forwarded-for", b"203.0.113.7"),
    (b"x-real-ip", b"203.0.113.7"),
    (b"forwarded", b"for=203.0.113.7"),
]


def _middleware(*, loopback_bound: bool):
    from menhir.api.auth import BearerAuthMiddleware

    async def _app(scope, receive, send):
        scope["reached_app"] = True

    return BearerAuthMiddleware(_app, operator_key="secret", loopback_bound=loopback_bound)


def _scope(path: str, *, peer: str, headers: list[tuple[bytes, bytes]] | None = None):
    return {
        "type": "http",
        "path": path,
        "client": (peer, 51000),
        "headers": list(headers or []),
        "query_string": b"",
    }


async def _passes_unauthenticated(mw, scope) -> bool:
    async def _receive():  # pragma: no cover - never awaited on the exempt path
        return {}

    sent: list[dict] = []

    async def _send(message):
        sent.append(message)

    await mw(scope, _receive, _send)
    if scope.get("reached_app"):
        return True
    statuses = [m.get("status") for m in sent if m.get("type") == "http.response.start"]
    return not any(s in (401, 403) for s in statuses)


@pytest.mark.asyncio
@pytest.mark.parametrize("header", _FORWARDING_HEADERS)
async def test_cf8_a_same_host_proxy_cannot_reach_the_explorer_unauthenticated(header) -> None:
    """`_loopback_admin_ok` is a static server-configuration boolean, permanently True on a
    loopback-bound server, so `(self._loopback_admin_ok or direct_loopback)` short-circuited and
    the only term excluding forwarded requests was never evaluated. A same-host reverse proxy
    connects from 127.0.0.1 and satisfied every remaining condition."""
    mw = _middleware(loopback_bound=True)
    scope = _scope("/explorer", peer="127.0.0.1", headers=[header])
    assert await _passes_unauthenticated(mw, scope) is False


@pytest.mark.asyncio
async def test_cf8_a_genuine_local_browser_still_reaches_the_explorer() -> None:
    """The explorer is a browser UI and a browser cannot attach a bearer token. Gating it on
    anything a real local visit fails would be a regression, not a hardening -- which is why the
    bind stays the boundary and only the forwarding term was added."""
    mw = _middleware(loopback_bound=True)
    scope = _scope("/explorer", peer="127.0.0.1")
    assert await _passes_unauthenticated(mw, scope) is True


@pytest.mark.asyncio
async def test_cf8_a_direct_loopback_client_on_a_network_bind_still_reaches_it() -> None:
    """The other documented case: a local connection to a LAN-bound server. This must keep
    working, and it is why the peer check is an `or` term rather than an additional `and`."""
    mw = _middleware(loopback_bound=False)
    scope = _scope("/explorer", peer="127.0.0.1")
    assert await _passes_unauthenticated(mw, scope) is True


@pytest.mark.asyncio
@pytest.mark.parametrize("header", _FORWARDING_HEADERS)
async def test_cf8_forwarding_is_excluded_on_a_network_bind_too(header) -> None:
    mw = _middleware(loopback_bound=False)
    scope = _scope("/explorer", peer="127.0.0.1", headers=[header])
    assert await _passes_unauthenticated(mw, scope) is False


@pytest.mark.asyncio
async def test_cf8_a_remote_client_is_still_enforced() -> None:
    mw = _middleware(loopback_bound=False)
    scope = _scope("/explorer", peer="203.0.113.7")
    assert await _passes_unauthenticated(mw, scope) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("header", _FORWARDING_HEADERS)
async def test_cf8_the_explorer_subtree_is_covered_not_just_its_root(header) -> None:
    """`is_explorer` matches the whole subtree, so the bypass covered every explorer route --
    including the candidate approve/reject writes, not only the read-only home page."""
    mw = _middleware(loopback_bound=True)
    scope = _scope("/explorer/candidates/approve", peer="127.0.0.1", headers=[header])
    assert await _passes_unauthenticated(mw, scope) is False
