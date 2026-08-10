"""Tests for the short-lived single-use authorization-code store (Phase 5)."""

from __future__ import annotations

import threading
import time

import pytest

from menhir.api.auth_code_store import AuthCodeStore, AuthCodeRecord, verify_pkce

pytestmark = pytest.mark.unit


def _issue(store, *, client_id="client-abc", redirect_uri="https://app.example.com/cb"):
    return store.issue(
        client_id=client_id,
        redirect_uri=redirect_uri,
        scope="menhir:read menhir:write",
        code_challenge="E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
        code_challenge_method="S256",
        resource="https://memory.example.com/mcp-http",
        subject="operator",
    )


def test_issue_then_redeem_happy_path(tmp_path):
    store = AuthCodeStore(tmp_path / "as.db")
    code = _issue(store)
    rec = store.redeem(
        code=code,
        client_id="client-abc",
        redirect_uri="https://app.example.com/cb",
    )
    assert rec is not None
    assert isinstance(rec, AuthCodeRecord)
    assert rec.client_id == "client-abc"
    assert rec.scope == "menhir:read menhir:write"
    assert rec.subject == "operator"
    assert rec.code_challenge_method == "S256"
    assert rec.redeemed_at is not None


def test_second_redeem_of_same_code_returns_none(tmp_path):
    store = AuthCodeStore(tmp_path / "as.db")
    code = _issue(store)
    first = store.redeem(
        code=code,
        client_id="client-abc",
        redirect_uri="https://app.example.com/cb",
    )
    assert first is not None
    second = store.redeem(
        code=code,
        client_id="client-abc",
        redirect_uri="https://app.example.com/cb",
    )
    assert second is None


def test_expired_code_returns_none(tmp_path):
    store = AuthCodeStore(tmp_path / "as.db", ttl_s=0.05)
    code = _issue(store)
    time.sleep(0.15)
    rec = store.redeem(
        code=code,
        client_id="client-abc",
        redirect_uri="https://app.example.com/cb",
    )
    assert rec is None


def test_wrong_client_id_returns_none(tmp_path):
    store = AuthCodeStore(tmp_path / "as.db")
    code = _issue(store)
    rec = store.redeem(
        code=code,
        client_id="someone-else",
        redirect_uri="https://app.example.com/cb",
    )
    assert rec is None


def test_wrong_redirect_uri_returns_none(tmp_path):
    store = AuthCodeStore(tmp_path / "as.db")
    code = _issue(store)
    rec = store.redeem(
        code=code,
        client_id="client-abc",
        redirect_uri="https://evil.example.com/cb",
    )
    assert rec is None


def test_unknown_code_returns_none(tmp_path):
    store = AuthCodeStore(tmp_path / "as.db")
    rec = store.redeem(
        code="never-issued",
        client_id="client-abc",
        redirect_uri="https://app.example.com/cb",
    )
    assert rec is None


def test_issue_rejects_non_s256_method(tmp_path):
    store = AuthCodeStore(tmp_path / "as.db")
    with pytest.raises(ValueError):
        store.issue(
            client_id="client-abc",
            redirect_uri="https://app.example.com/cb",
            scope="menhir:read menhir:write",
            code_challenge="E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
            code_challenge_method="plain",
            resource="https://memory.example.com/mcp-http",
            subject="operator",
        )


def test_verify_pkce_rfc7636_vector(tmp_path):
    assert (
        verify_pkce(
            "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk",
            "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
        )
        is True
    )
    assert (
        verify_pkce(
            "wrong-verifier",
            "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
        )
        is False
    )
    assert (
        verify_pkce(
            "",
            "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
        )
        is False
    )


def test_code_not_stored_in_plaintext(tmp_path):
    store = AuthCodeStore(tmp_path / "as.db")
    code = _issue(store)
    raw = (tmp_path / "as.db").read_bytes()
    assert code.encode() not in raw


def test_concurrent_redeem_exactly_one_wins(tmp_path):
    store = AuthCodeStore(tmp_path / "as.db")
    code = _issue(store)
    results = []
    errors = []
    barrier = threading.Barrier(2)

    def redeem_code():
        try:
            barrier.wait()
            result = store.redeem(
                code=code,
                client_id="client-abc",
                redirect_uri="https://app.example.com/cb",
            )
            results.append(result)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=redeem_code) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert sum(1 for r in results if r is not None) == 1
    assert sum(1 for r in results if r is None) == 1
