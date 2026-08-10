"""Request correlation helpers for HTTP/API surfaces."""

from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar, Token
from uuid import uuid4

type ASGIApp = Callable
type Scope = dict
type Receive = Callable
type Send = Callable
type Message = dict

_REQUEST_ID_HEADER = b"x-request-id"
_request_id_var: ContextVar[str | None] = ContextVar("menhir_request_id", default=None)


def get_request_id() -> str | None:
    """Return the currently bound request id, if any."""

    return _request_id_var.get()


def get_request_id_from_scope(scope: Scope) -> str | None:
    """Return a request id from ASGI scope state, if present."""

    state = scope.get("state")
    if isinstance(state, dict):
        value = state.get("request_id")
        if isinstance(value, str) and value:
            return value
    return None


def _ensure_scope_request_id(scope: Scope) -> str:
    headers = dict(scope.get("headers", []))
    header_value = headers.get(_REQUEST_ID_HEADER, b"").decode("latin-1").strip()
    request_id = header_value or uuid4().hex
    state = scope.setdefault("state", {})
    if isinstance(state, dict):
        state["request_id"] = request_id
    return request_id


class RequestContextMiddleware:
    """Bind a request id and echo it back on HTTP responses."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        request_id = _ensure_scope_request_id(scope)
        token: Token[str | None] = _request_id_var.set(request_id)

        async def send_with_request_id(message: Message) -> None:
            if message.get("type") == "http.response.start":
                headers = list(message.get("headers", []))
                if not any(name.lower() == _REQUEST_ID_HEADER for name, _ in headers):
                    headers.append([_REQUEST_ID_HEADER, request_id.encode("latin-1")])
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            _request_id_var.reset(token)
