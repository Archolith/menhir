"""Tests for OAuth local smoke helper.

No real server — mocks all HTTP calls.
"""

from __future__ import annotations

import importlib.util
import json
import urllib.error
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def _load_smoke():
    path = Path(__file__).resolve().parents[1] / "scripts" / "smoke" / "oauth_local_smoke.py"
    spec = importlib.util.spec_from_file_location("oauth_local_smoke", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeHTTPResponse:
    def __init__(self, data, status=200, headers=None):
        self.data = data
        self.status = status
        self.headers = headers or {}

    def read(self):
        return json.dumps(self.data).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def close(self):
        pass


def _fake_urlopen_factory(routes):
    """Build a urlopen replacement from an ordered list of (prefix, handler).

    handlers are either FakeHTTPResponse or a callable(req, url).
    First matching prefix wins.
    """

    def urlopen(req, **kw):
        url = str(getattr(req, "full_url", req))
        for prefix, handler in routes:
            if prefix in url:
                if callable(handler):
                    return handler(req, url)
                return handler
        return FakeHTTPResponse({})

    return urlopen


def _make_http_error(status, body, headers=None):
    """Return a callable that raises HTTPError like urlopen would."""

    def _raise(req, url):
        fp = FakeHTTPResponse(body, status=status, headers=headers or {})
        raise urllib.error.HTTPError(
            url, status, f"HTTP {status}", fp.headers, fp
        )

    return _raise


def _make_os_error(msg):
    def _raise(req, url):
        raise OSError(msg)
    return _raise


# ---------------------------------------------------------------------------
# Test data
# ---------------------------------------------------------------------------

_METADATA_OK = {
    "resource": "https://memory.example.com/mcp-http",
    "authorization_servers": ["https://auth.example.com"],
    "scopes_supported": ["menhir:read", "menhir:write", "menhir:admin"],
    "bearer_methods_supported": ["header"],
    "resource_name": "Menhir MCP",
}

_BEARER_CHALLENGE_HEADERS = {
    "www-authenticate": (
        'Bearer resource_metadata="http://127.0.0.1:8100'
        '/.well-known/oauth-protected-resource", '
        'error="invalid_token", error_description="Access token required"'
    ),
}

_BEARER_NO_RESOURCE_META_HEADERS = {
    "www-authenticate": 'Bearer error="invalid_token"',
}

_BEARER_CHALLENGE_UPPERCASE_HEADERS = {
    "WWW-Authenticate": (
        'Bearer resource_metadata="http://127.0.0.1:8100'
        '/.well-known/oauth-protected-resource", '
        'error="invalid_token", error_description="Access token required"'
    ),
}

_BEARER_CHALLENGE_MIXEDCASE_HEADERS = {
    "Www-Authenticate": (
        'Bearer resource_metadata="http://127.0.0.1:8100'
        '/.well-known/oauth-protected-resource", '
        'error="invalid_token", error_description="Access token required"'
    ),
}


_METADATA_PREFIX = "/.well-known/oauth-protected-resource"
_MCP_BARE = "/mcp"  # must be after /mcp? and /mcp-http
_MCP_HTTP_BARE = "/mcp-http"
_MCP_API_KEY = "/mcp?api_key="
_MCP_HTTP_API_KEY = "mcp-http?api_key="


def _all_pass_routes():
    """Ordered route list: specific prefixes before generic ones."""
    return [
        (_METADATA_PREFIX, FakeHTTPResponse(_METADATA_OK)),
        (_MCP_HTTP_API_KEY, _make_http_error(401, {"error": "Unauthorized"}, headers=_BEARER_CHALLENGE_HEADERS)),
        (_MCP_API_KEY, _make_http_error(401, {"error": "Unauthorized"}, headers=_BEARER_CHALLENGE_HEADERS)),
        (_MCP_HTTP_BARE, _make_http_error(401, {"error": "Unauthorized"}, headers=_BEARER_CHALLENGE_HEADERS)),
        (_MCP_BARE, _make_http_error(401, {"error": "Unauthorized"}, headers=_BEARER_CHALLENGE_HEADERS)),
    ]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_all_checks_pass(monkeypatch):
    smoke = _load_smoke()
    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen_factory(_all_pass_routes()))
    assert smoke.main() == 0


def test_metadata_non_200_fails(monkeypatch):
    smoke = _load_smoke()
    routes = [(_METADATA_PREFIX, _make_http_error(404, {"detail": "Not Found"}))]
    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen_factory(routes))
    assert smoke.main() == 1


def test_metadata_missing_fields_fails(monkeypatch):
    smoke = _load_smoke()
    routes = [(_METADATA_PREFIX, FakeHTTPResponse(
        {"resource": "https://memory.example.com/mcp-http"}, status=200
    ))]
    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen_factory(routes))
    assert smoke.main() == 1


def test_mcp_no_www_authenticate_fails(monkeypatch):
    smoke = _load_smoke()
    routes = [
        (_METADATA_PREFIX, FakeHTTPResponse(_METADATA_OK)),
        (_MCP_HTTP_API_KEY, _make_http_error(401, {"error": "Unauthorized"}, headers=_BEARER_CHALLENGE_HEADERS)),
        (_MCP_API_KEY, _make_http_error(401, {"error": "Unauthorized"}, headers=_BEARER_CHALLENGE_HEADERS)),
        (_MCP_HTTP_BARE, _make_http_error(401, {"error": "Unauthorized"}, headers=_BEARER_CHALLENGE_HEADERS)),
        (_MCP_BARE, _make_http_error(401, {"error": "Unauthorized"})),
    ]
    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen_factory(routes))
    assert smoke.main() == 1


def test_mcp_api_key_200_fails(monkeypatch):
    smoke = _load_smoke()
    routes = [
        (_METADATA_PREFIX, FakeHTTPResponse(_METADATA_OK)),
        (_MCP_HTTP_API_KEY, _make_http_error(401, {"error": "Unauthorized"}, headers=_BEARER_CHALLENGE_HEADERS)),
        (_MCP_API_KEY, FakeHTTPResponse({"ok": True}, status=200)),
        (_MCP_HTTP_BARE, _make_http_error(401, {"error": "Unauthorized"}, headers=_BEARER_CHALLENGE_HEADERS)),
        (_MCP_BARE, _make_http_error(401, {"error": "Unauthorized"}, headers=_BEARER_CHALLENGE_HEADERS)),
    ]
    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen_factory(routes))
    assert smoke.main() == 1


def test_mcp_http_api_key_200_fails(monkeypatch):
    smoke = _load_smoke()
    routes = [
        (_METADATA_PREFIX, FakeHTTPResponse(_METADATA_OK)),
        (_MCP_HTTP_API_KEY, FakeHTTPResponse({"ok": True}, status=200)),
        (_MCP_API_KEY, _make_http_error(401, {"error": "Unauthorized"}, headers=_BEARER_CHALLENGE_HEADERS)),
        (_MCP_HTTP_BARE, _make_http_error(401, {"error": "Unauthorized"}, headers=_BEARER_CHALLENGE_HEADERS)),
        (_MCP_BARE, _make_http_error(401, {"error": "Unauthorized"}, headers=_BEARER_CHALLENGE_HEADERS)),
    ]
    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen_factory(routes))
    assert smoke.main() == 1


def test_mcp_api_key_no_www_authenticate_fails(monkeypatch):
    """?api_key= returns 401 but no WWW-Authenticate → fail."""
    smoke = _load_smoke()
    routes = [
        (_METADATA_PREFIX, FakeHTTPResponse(_METADATA_OK)),
        (_MCP_HTTP_API_KEY, _make_http_error(401, {"error": "Unauthorized"}, headers=_BEARER_CHALLENGE_HEADERS)),
        (_MCP_API_KEY, _make_http_error(401, {"error": "Unauthorized"})),
        (_MCP_HTTP_BARE, _make_http_error(401, {"error": "Unauthorized"}, headers=_BEARER_CHALLENGE_HEADERS)),
        (_MCP_BARE, _make_http_error(401, {"error": "Unauthorized"}, headers=_BEARER_CHALLENGE_HEADERS)),
    ]
    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen_factory(routes))
    assert smoke.main() == 1


def test_mcp_http_api_key_no_www_authenticate_fails(monkeypatch):
    """?api_key= on /mcp-http returns 401 but no WWW-Authenticate → fail."""
    smoke = _load_smoke()
    routes = [
        (_METADATA_PREFIX, FakeHTTPResponse(_METADATA_OK)),
        (_MCP_HTTP_API_KEY, _make_http_error(401, {"error": "Unauthorized"})),
        (_MCP_API_KEY, _make_http_error(401, {"error": "Unauthorized"}, headers=_BEARER_CHALLENGE_HEADERS)),
        (_MCP_HTTP_BARE, _make_http_error(401, {"error": "Unauthorized"}, headers=_BEARER_CHALLENGE_HEADERS)),
        (_MCP_BARE, _make_http_error(401, {"error": "Unauthorized"}, headers=_BEARER_CHALLENGE_HEADERS)),
    ]
    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen_factory(routes))
    assert smoke.main() == 1


def test_mcp_bare_no_resource_metadata_fails(monkeypatch):
    """Bearer challenge without resource_metadata= → fail."""
    smoke = _load_smoke()
    routes = [
        (_METADATA_PREFIX, FakeHTTPResponse(_METADATA_OK)),
        (_MCP_HTTP_API_KEY, _make_http_error(401, {"error": "Unauthorized"}, headers=_BEARER_CHALLENGE_HEADERS)),
        (_MCP_API_KEY, _make_http_error(401, {"error": "Unauthorized"}, headers=_BEARER_CHALLENGE_HEADERS)),
        (_MCP_HTTP_BARE, _make_http_error(401, {"error": "Unauthorized"}, headers=_BEARER_CHALLENGE_HEADERS)),
        (_MCP_BARE, _make_http_error(401, {"error": "Unauthorized"}, headers=_BEARER_NO_RESOURCE_META_HEADERS)),
    ]
    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen_factory(routes))
    assert smoke.main() == 1


def test_mcp_http_bare_no_resource_metadata_fails(monkeypatch):
    """Bearer challenge without resource_metadata= on /mcp-http → fail."""
    smoke = _load_smoke()
    routes = [
        (_METADATA_PREFIX, FakeHTTPResponse(_METADATA_OK)),
        (_MCP_HTTP_API_KEY, _make_http_error(401, {"error": "Unauthorized"}, headers=_BEARER_CHALLENGE_HEADERS)),
        (_MCP_API_KEY, _make_http_error(401, {"error": "Unauthorized"}, headers=_BEARER_CHALLENGE_HEADERS)),
        (_MCP_HTTP_BARE, _make_http_error(401, {"error": "Unauthorized"}, headers=_BEARER_NO_RESOURCE_META_HEADERS)),
        (_MCP_BARE, _make_http_error(401, {"error": "Unauthorized"}, headers=_BEARER_CHALLENGE_HEADERS)),
    ]
    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen_factory(routes))
    assert smoke.main() == 1


def test_api_key_no_resource_metadata_fails(monkeypatch):
    """?api_key= rejection without resource_metadata= → fail."""
    smoke = _load_smoke()
    routes = [
        (_METADATA_PREFIX, FakeHTTPResponse(_METADATA_OK)),
        (_MCP_HTTP_API_KEY, _make_http_error(401, {"error": "Unauthorized"}, headers=_BEARER_CHALLENGE_HEADERS)),
        (_MCP_API_KEY, _make_http_error(401, {"error": "Unauthorized"}, headers=_BEARER_NO_RESOURCE_META_HEADERS)),
        (_MCP_HTTP_BARE, _make_http_error(401, {"error": "Unauthorized"}, headers=_BEARER_CHALLENGE_HEADERS)),
        (_MCP_BARE, _make_http_error(401, {"error": "Unauthorized"}, headers=_BEARER_CHALLENGE_HEADERS)),
    ]
    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen_factory(routes))
    assert smoke.main() == 1


def test_www_authenticate_uppercase_header_tolerated(monkeypatch):
    """WWW-Authenticate (uppercase W) is tolerated."""
    smoke = _load_smoke()
    routes = [
        (_METADATA_PREFIX, FakeHTTPResponse(_METADATA_OK)),
        (_MCP_HTTP_API_KEY, _make_http_error(401, {"error": "Unauthorized"}, headers=_BEARER_CHALLENGE_UPPERCASE_HEADERS)),
        (_MCP_API_KEY, _make_http_error(401, {"error": "Unauthorized"}, headers=_BEARER_CHALLENGE_UPPERCASE_HEADERS)),
        (_MCP_HTTP_BARE, _make_http_error(401, {"error": "Unauthorized"}, headers=_BEARER_CHALLENGE_UPPERCASE_HEADERS)),
        (_MCP_BARE, _make_http_error(401, {"error": "Unauthorized"}, headers=_BEARER_CHALLENGE_UPPERCASE_HEADERS)),
    ]
    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen_factory(routes))
    assert smoke.main() == 0


def test_www_authenticate_mixedcase_header_tolerated(monkeypatch):
    """Www-Authenticate (mixed case) is tolerated."""
    smoke = _load_smoke()
    routes = [
        (_METADATA_PREFIX, FakeHTTPResponse(_METADATA_OK)),
        (_MCP_HTTP_API_KEY, _make_http_error(401, {"error": "Unauthorized"}, headers=_BEARER_CHALLENGE_MIXEDCASE_HEADERS)),
        (_MCP_API_KEY, _make_http_error(401, {"error": "Unauthorized"}, headers=_BEARER_CHALLENGE_MIXEDCASE_HEADERS)),
        (_MCP_HTTP_BARE, _make_http_error(401, {"error": "Unauthorized"}, headers=_BEARER_CHALLENGE_MIXEDCASE_HEADERS)),
        (_MCP_BARE, _make_http_error(401, {"error": "Unauthorized"}, headers=_BEARER_CHALLENGE_MIXEDCASE_HEADERS)),
    ]
    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen_factory(routes))
    assert smoke.main() == 0


def test_connection_error_fails(monkeypatch):
    smoke = _load_smoke()
    routes = [(_METADATA_PREFIX, _make_os_error("Connection refused"))]
    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen_factory(routes))
    assert smoke.main() == 1


def test_no_secrets_in_output(monkeypatch, capsys):
    smoke = _load_smoke()
    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen_factory(_all_pass_routes()))
    assert smoke.main([]) == 0
    out = capsys.readouterr().out
    for sentinel in ("super-secret-oauth-token", "super-secret-client-secret", "Bearer ey"):
        assert sentinel not in out, f"secret sentinel leaked: {sentinel}"


def test_json_output_contains_checks(monkeypatch, capsys):
    smoke = _load_smoke()
    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen_factory(_all_pass_routes()))
    assert smoke.main(["--json"]) == 0
    out = capsys.readouterr().out.strip()
    parsed = json.loads(out)
    assert parsed["all_pass"] is True
    assert len(parsed["checks"]) == 5
    assert all(c["status"] == "PASS" for c in parsed["checks"])


def test_default_base_url(monkeypatch):
    monkeypatch.delenv("MENHIR_SMOKE_URL", raising=False)
    smoke = _load_smoke()
    assert smoke._resolve_base_url() == "http://127.0.0.1:8100"


def test_base_url_env_var(monkeypatch):
    monkeypatch.setenv("MENHIR_SMOKE_URL", "http://localhost:8080")
    smoke = _load_smoke()
    assert smoke._resolve_base_url() == "http://localhost:8080"


def test_base_url_flag(monkeypatch, capsys):
    monkeypatch.delenv("MENHIR_SMOKE_URL", raising=False)
    smoke = _load_smoke()
    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen_factory(_all_pass_routes()))
    assert smoke.main(["--base-url", "http://custom:9090"]) == 0
    out = capsys.readouterr().out
    assert "custom:9090" in out


def test_failure_json_includes_failures(monkeypatch, capsys):
    smoke = _load_smoke()
    routes = [(_METADATA_PREFIX, _make_http_error(404, {"detail": "Not Found"}))]
    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen_factory(routes))
    assert smoke.main(["--json"]) == 1
    out = capsys.readouterr().out.strip()
    parsed = json.loads(out)
    assert parsed["all_pass"] is False
    assert parsed["checks"][0]["status"] == "FAIL"
