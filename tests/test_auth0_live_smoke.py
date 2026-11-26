"""Offline tests for the optional Auth0 live smoke CLI."""

from __future__ import annotations

import contextlib
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


def _load_smoke():
    path = Path(__file__).resolve().parents[1] / "scripts" / "smoke" / "auth0_live_smoke.py"
    spec = importlib.util.spec_from_file_location("auth0_live_smoke", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_unconfigured_credentials_skip(monkeypatch, capsys):
    smoke = _load_smoke()
    for name in ("AUTH0_DOMAIN", "AUTH0_CLIENT_ID", "AUTH0_CLIENT_SECRET", "AUTH0_AUDIENCE"):
        monkeypatch.delenv(name, raising=False)

    assert smoke.main() == 0
    assert "[SKIP]" in capsys.readouterr().out


def test_partial_credentials_fail_without_secret(monkeypatch, capsys):
    smoke = _load_smoke()
    monkeypatch.setenv("AUTH0_DOMAIN", "tenant.example")
    monkeypatch.setenv("AUTH0_CLIENT_ID", "client-id")
    monkeypatch.delenv("AUTH0_CLIENT_SECRET", raising=False)
    monkeypatch.setenv("AUTH0_AUDIENCE", "https://menhir.example/api")

    assert smoke.main() == 1
    output = capsys.readouterr().out
    assert "partially configured" in output
    assert "client-secret" not in output


@pytest.mark.parametrize(("valid_request_status", "exit_status"), [(503, 0), (None, 1)])
def test_wrong_audience_grant_absence_is_explicit_optional_skip(
    monkeypatch, capsys, valid_request_status, exit_status,
):
    smoke = _load_smoke()
    for name, value in {
        "AUTH0_DOMAIN": "tenant.example",
        "AUTH0_CLIENT_ID": "client-id",
        "AUTH0_CLIENT_SECRET": "client-secret",
        "AUTH0_AUDIENCE": "https://menhir.example/api",
    }.items():
        monkeypatch.setenv(name, value)

    minted = iter(["good-token", RuntimeError("vendor-secret-body")])

    def fake_mint(*_args):
        value = next(minted)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(smoke, "_mint_token", fake_mint)

    def fake_req(url, **kwargs):
        if kwargs.get("headers", {}).get("Authorization") == "Bearer good-token":
            return valid_request_status, {}, "backend unavailable"
        return 401, {"www-authenticate": 'Bearer resource_metadata="meta"'}, ""

    monkeypatch.setattr(smoke, "_req", fake_req)

    @contextlib.contextmanager
    def fake_launch(*_args, **_kwargs):
        yield SimpleNamespace(base_url="http://stub")

    monkeypatch.setattr(smoke, "launch", fake_launch)

    assert smoke.main() == exit_status
    output = capsys.readouterr().out
    assert "[SKIP] auth0: wrong-audience token -> 401  (Management API token unavailable)" in output
    assert "vendor-secret-body" not in output
