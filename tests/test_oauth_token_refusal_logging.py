"""#541: every /oauth/token refusal logs why and for which client, and never logs the secrets."""

from __future__ import annotations

import logging

import pytest

from tests.test_oauth_token import (  # noqa: F401 - _isolate is an autouse fixture
    _ENABLED_REFRESH,
    _SCOPE,
    _client,
    _isolate,
    _issue_initial_refresh,
    _pkce,
    _policy,
    _refresh_form,
    _register_client,
    _seed_code,
    _token_form,
)

pytestmark = pytest.mark.unit

_LOGGER = "menhir.api.oauth_token"


def _refusals(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == _LOGGER]


def _all_text(caplog) -> str:
    return "\n".join(r.getMessage() for r in caplog.records)


def test_invalid_request_logs_one_warning(caplog):
    verifier, challenge = _pkce()
    cid = _register_client()
    form = _token_form(_seed_code(cid, challenge), cid, verifier)
    del form["code_verifier"]
    with caplog.at_level(logging.DEBUG):
        assert _client().post("/oauth/token", data=form).status_code == 400
    [record] = _refusals(caplog)
    assert record.levelno == logging.WARNING
    assert "error=invalid_request" in record.getMessage()
    assert "grant_type=authorization_code" in record.getMessage()
    assert f"client={cid}" in record.getMessage()


def test_invalid_grant_logs_one_warning(caplog):
    verifier, _ = _pkce()
    cid = _register_client()
    with caplog.at_level(logging.DEBUG):
        resp = _client().post("/oauth/token", data=_token_form("no-such-code", cid, verifier))
    assert resp.json()["error"] == "invalid_grant"
    [record] = _refusals(caplog)
    assert record.levelno == logging.WARNING
    assert "error=invalid_grant" in record.getMessage()


def test_unknown_client_under_policy_logs_raw_client_id(caplog):
    verifier, challenge = _pkce()
    cid = _register_client()
    code = _seed_code(cid, challenge)
    with caplog.at_level(logging.DEBUG):
        resp = _client(policy=_policy("different-client")).post(
            "/oauth/token", data=_token_form(code, cid, verifier)
        )
    assert resp.json()["error"] == "unauthorized_client"
    [record] = _refusals(caplog)
    assert "error=unauthorized_client" in record.getMessage()
    assert f"client={cid}" in record.getMessage()


def test_refresh_scope_mismatch_logs_policy_label_and_scopes(caplog):
    """The #533 shape: a refresh whose scopes differ from the client's exact policy."""
    c, cid, initial = _issue_initial_refresh()
    token = str(initial["refresh_token"])
    c.app.state.client_policy = _policy(cid)  # policy now demands exactly read + write
    with caplog.at_level(logging.DEBUG):
        resp = c.post("/oauth/token", data=_refresh_form(token, cid, scope=_SCOPE))
    assert resp.status_code == 400
    assert resp.json()["error"] == "unauthorized_client"
    [record] = _refusals(caplog)
    message = record.getMessage()
    assert record.levelno == logging.WARNING
    assert "grant_type=refresh_token" in message
    assert "client=chatgpt-chat" in message
    assert "scopes=menhir:read menhir:write menhir:admin" in message


def test_unsupported_grant_type_logs_one_warning(caplog):
    with caplog.at_level(logging.DEBUG):
        _client().post("/oauth/token", data={"grant_type": "password", "client_id": "x"})
    [record] = _refusals(caplog)
    assert "error=unsupported_grant_type" in record.getMessage()
    assert "grant_type=password" in record.getMessage()


def test_client_supplied_fields_cannot_forge_log_lines(caplog):
    with caplog.at_level(logging.DEBUG):
        _client().post(
            "/oauth/token",
            data={"grant_type": "password\nFAKE ok", "client_id": "a\r\nb"},
        )
    [record] = _refusals(caplog)
    assert "\n" not in record.getMessage()
    assert "\r" not in record.getMessage()


def test_secrets_never_reach_any_log_record(caplog):
    verifier, challenge = _pkce()
    cid = _register_client()
    code = _seed_code(cid, challenge)
    with caplog.at_level(logging.DEBUG):
        # Policy refusal with a real code and verifier.
        _client(policy=_policy("different-client")).post(
            "/oauth/token", data=_token_form(code, cid, verifier)
        )
        # PKCE refusal with the real code.
        _client().post("/oauth/token", data=_token_form(code, cid, "b" * 64))
        # Refresh refusals with a real refresh token.
        c, rcid, initial = _issue_initial_refresh()
        token = str(initial["refresh_token"])
        c.post("/oauth/token", data=_refresh_form(token, rcid, resource="https://wrong"))
        c.app.state.client_policy = _policy(rcid)
        c.post("/oauth/token", data=_refresh_form(token, rcid, scope=_SCOPE))
    assert len(_refusals(caplog)) == 4
    text = _all_text(caplog)
    for secret in (code, verifier, token):
        assert secret not in text


def test_success_logs_no_refusal(caplog):
    verifier, challenge = _pkce()
    cid = _register_client()
    code = _seed_code(cid, challenge, scope=f"{_SCOPE} offline_access")
    with caplog.at_level(logging.DEBUG):
        resp = _client(_ENABLED_REFRESH).post("/oauth/token", data=_token_form(code, cid, verifier))
    assert resp.status_code == 200
    assert _refusals(caplog) == []
