"""Offline tests for the Auth0 Management API diagnostic."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


def _load_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "dev" / "auth0_diagnose.py"
    spec = importlib.util.spec_from_file_location("auth0_diagnose", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Response:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    def read(self):
        return self._body if isinstance(self._body, bytes) else json.dumps(self._body).encode()


class _Connection:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []
        self.closed = False

    def request(self, method, path, body=None, headers=None):
        self.requests.append((method, path))

    def getresponse(self):
        return next(self.responses)

    def close(self):
        self.closed = True


def _run(monkeypatch, responses, capsys):
    diagnose = _load_module()
    connection = _Connection(responses)
    monkeypatch.setenv("AUTH0_DOMAIN", "tenant.example")
    monkeypatch.setenv("AUTH0_MGMT_CLIENT_ID", "client-id")
    monkeypatch.setenv("AUTH0_MGMT_CLIENT_SECRET", "client-secret")
    monkeypatch.setattr(diagnose.http.client, "HTTPSConnection", lambda _domain: connection)
    result = diagnose.main()
    return result, connection, capsys.readouterr()


def test_success_uses_hyphenated_management_paths(monkeypatch, capsys):
    result, connection, output = _run(
        monkeypatch,
        [
            _Response(200, {"access_token": "token"}),
            _Response(200, [{"name": "API", "identifier": "aud", "scopes": []}]),
            _Response(200, [{"client_id": "cid", "audience": "aud", "scope": []}]),
            _Response(200, [{"client_id": "cid", "name": "App", "app_type": "native"}]),
        ],
        capsys,
    )

    assert result == 0
    assert [path for _, path in connection.requests] == [
        "/oauth/token",
        "/api/v2/resource-servers",
        "/api/v2/client-grants",
        "/api/v2/clients?fields=client_id,name,app_type&include_fields=true",
    ]
    assert "=== Applications (clients) ===" in output.out
    assert connection.closed is True


def test_non_success_reports_concise_error_without_vendor_body(monkeypatch, capsys):
    result, _, output = _run(
        monkeypatch,
        [
            _Response(200, {"access_token": "token"}),
            _Response(403, {"message": "vendor-secret-error"}),
        ],
        capsys,
    )

    assert result == 1
    assert "resource servers request failed (HTTP 403)" in output.err
    assert "vendor-secret-error" not in output.err


def test_unexpected_payload_reports_error_instead_of_empty_section(monkeypatch, capsys):
    result, _, output = _run(
        monkeypatch,
        [_Response(200, {"access_token": "token"}), _Response(200, {"not": "a-list"})],
        capsys,
    )

    assert result == 1
    assert "resource servers returned an unexpected payload" in output.err


@pytest.mark.parametrize(
    "token_body, server_body, expected",
    [
        ({"access_token": 42}, [], "management token returned an unexpected payload"),
        ({"access_token": "token"}, ["malformed"], "resource servers returned malformed entries"),
        (
            {"access_token": "token"},
            [{"name": "API", "identifier": "aud", "scopes": ["malformed"]}],
            "resource servers returned malformed scopes",
        ),
    ],
)
def test_malformed_responses_fail_without_body_or_secret(
    monkeypatch, capsys, token_body, server_body, expected
):
    result, connection, output = _run(
        monkeypatch,
        [
            _Response(200, token_body),
            _Response(200, server_body),
        ],
        capsys,
    )

    assert result == 1
    assert expected in output.err
    assert "client-secret" not in output.out + output.err
    assert connection.closed is True
