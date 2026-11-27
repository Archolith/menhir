from __future__ import annotations

import importlib.util
import json
import sys
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "remote_sim_healthcheck", ROOT / "deploy" / "remote_sim_healthcheck.py"
)
assert SPEC is not None and SPEC.loader is not None
healthcheck = importlib.util.module_from_spec(SPEC)
sys.modules["remote_sim_healthcheck"] = healthcheck
SPEC.loader.exec_module(healthcheck)


class _Response:
    def __init__(self, body: str, status: int = 200) -> None:
        self.status = status
        self._body = body.encode()

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def _patch(monkeypatch: pytest.MonkeyPatch, outcome) -> list:
    seen: list = []

    def fake_urlopen(request, timeout=None):
        seen.append(request)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(healthcheck.urllib.request, "urlopen", fake_urlopen)
    return seen


_OK = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"tools": []}})


def test_plain_json_result_is_healthy(monkeypatch) -> None:
    _patch(monkeypatch, _Response(_OK))
    assert healthcheck.main() == 0


def test_sse_result_is_healthy(monkeypatch) -> None:
    _patch(monkeypatch, _Response(f"event: message\ndata: {_OK}\n\n"))
    assert healthcheck.main() == 0


def test_http_200_jsonrpc_error_is_unhealthy(monkeypatch, capsys) -> None:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": "no"}})
    _patch(monkeypatch, _Response(body))
    assert healthcheck.main() == 1
    assert capsys.readouterr().err.strip().count("\n") == 0


def test_non_200_is_unhealthy_with_one_line_reason(monkeypatch, capsys) -> None:
    _patch(monkeypatch, _Response("", status=503))
    assert healthcheck.main() == 1
    err = capsys.readouterr().err
    assert "503" in err and err.strip().count("\n") == 0


def test_connection_error_is_unhealthy_with_one_line_reason(monkeypatch, capsys) -> None:
    _patch(monkeypatch, urllib.error.URLError("refused"))
    assert healthcheck.main() == 1
    err = capsys.readouterr().err
    assert "refused" in err and err.strip().count("\n") == 0


def test_env_overrides_port_and_key(monkeypatch) -> None:
    monkeypatch.setenv("MENHIR_API_PORT", "9123")
    monkeypatch.setenv("MENHIR_OPERATOR_KEY", "other-key")
    seen = _patch(monkeypatch, _Response(_OK))
    assert healthcheck.main() == 0
    assert seen[0].full_url == "http://127.0.0.1:9123/mcp-http"
    assert seen[0].get_header("Authorization") == "Bearer other-key"
    assert seen[0].get_header("User-agent") == "menhir-remote-sim-healthcheck"


def test_defaults_without_env(monkeypatch) -> None:
    monkeypatch.delenv("MENHIR_API_PORT", raising=False)
    monkeypatch.delenv("MENHIR_OPERATOR_KEY", raising=False)
    seen = _patch(monkeypatch, _Response(_OK))
    assert healthcheck.main() == 0
    assert seen[0].full_url == "http://127.0.0.1:8099/mcp-http"
    assert seen[0].get_header("Authorization") == "Bearer sim-operator-key"
