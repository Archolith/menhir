"""#669: which code an unexpected exception gets. A retryable code on a deterministic bug invites
retries; a bad-input code on a server fault hides it from the E2E fault check."""

from __future__ import annotations

import json
import sqlite3

import httpx
import pytest

from menhir.mcp.telemetry.tracker import McpToolRefusal, failure_code

pytestmark = [pytest.mark.unit]


def _status_error(code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://127.0.0.1:8090/api/internal/backend/x")
    return httpx.HTTPStatusError("x", request=request, response=httpx.Response(code, request=request))


def _named(name: str) -> Exception:
    return type(name, (Exception,), {})("boom")


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (None, "timeout"),
        (McpToolRefusal("no", code="not_found"), "not_found"),
        (PermissionError("no"), "access_denied"),
        (httpx.ConnectError("refused"), "unavailable"),
        (_status_error(503), "unavailable"),
        (_status_error(502), "unavailable"),
        (_status_error(429), "unavailable"),
        (_status_error(500), "internal"),  # a server bug: retrying will not help
        (_named("ServiceUnavailable"), "unavailable"),
        (_named("DatabaseUnavailable"), "unavailable"),
        (_named("TransientError"), "unavailable"),
        (sqlite3.OperationalError("database is locked"), "unavailable"),
        (json.JSONDecodeError("bad", "{", 0), "internal"),  # a malformed response, not bad input
        (ValueError("limit must be positive"), "invalid_argument"),
        (RuntimeError("neo4j says the query mentions neo4j"), "internal"),
    ],
    ids=lambda v: v if isinstance(v, str) else type(v).__name__,
)
def test_failure_code(exc, code) -> None:
    assert failure_code(exc) == code
