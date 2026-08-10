"""Tests for AS-003: the consent/one-click HMAC secret is stable across workers.

Without an explicit ``MENHIR_OAUTH_AS_CONSENT_SECRET`` the secret is derived from the
persisted signing-key file (deterministic across processes/restarts) rather than a
per-process random value, and an operator preflight warns when it is unset.
"""

from __future__ import annotations

import os
from hashlib import sha256

import pytest

from menhir.api import oauth_authorize
from menhir.config import MemorySettings
from menhir.operator_diagnostics import build_operator_diagnostics

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _reset_persistent_secret_cache(monkeypatch):
    monkeypatch.setattr(oauth_authorize, "_PERSISTENT_CONSENT_SECRET", None, raising=False)
    monkeypatch.delenv("MENHIR_OAUTH_AS_CONSENT_SECRET", raising=False)
    yield
    monkeypatch.setattr(oauth_authorize, "_PERSISTENT_CONSENT_SECRET", None, raising=False)


def _write_key(tmp_path) -> bytes:
    key_bytes = b'{"kty":"RSA","kid":"test","n":"abc","e":"AQAB","d":"secret"}'
    (tmp_path / "oauth_signing_key.json").write_bytes(key_bytes)
    return key_bytes


def test_persistent_secret_is_disk_derived(tmp_path, monkeypatch):
    monkeypatch.setenv("MENHIR_OAUTH_AS_DIR", str(tmp_path))
    key_bytes = _write_key(tmp_path)
    expected = sha256(b"menhir-as-consent-v1\0" + key_bytes).digest()
    assert oauth_authorize._consent_secret() == expected


def test_secret_independent_of_process_random(tmp_path, monkeypatch):
    """Two workers with different per-process randoms derive the SAME secret from disk, so
    a session signed by one verifies under the other (multi-worker one-click works)."""
    monkeypatch.setenv("MENHIR_OAUTH_AS_DIR", str(tmp_path))
    _write_key(tmp_path)

    # Worker A
    monkeypatch.setattr(oauth_authorize, "_PROCESS_CONSENT_SECRET", os.urandom(32))
    monkeypatch.setattr(oauth_authorize, "_PERSISTENT_CONSENT_SECRET", None)
    token = oauth_authorize._sign_session("menhir-admin", ("cid",))

    # Worker B (different process random, same key file)
    monkeypatch.setattr(oauth_authorize, "_PROCESS_CONSENT_SECRET", os.urandom(32))
    monkeypatch.setattr(oauth_authorize, "_PERSISTENT_CONSENT_SECRET", None)
    verified = oauth_authorize._verify_session(token)
    assert verified == ("menhir-admin", ("cid",))


def test_explicit_env_secret_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("MENHIR_OAUTH_AS_DIR", str(tmp_path))
    _write_key(tmp_path)
    monkeypatch.setenv("MENHIR_OAUTH_AS_CONSENT_SECRET", "explicit-shared-secret")
    assert oauth_authorize._consent_secret() == b"explicit-shared-secret"


def test_fallback_when_key_missing_not_cached(tmp_path, monkeypatch):
    """With no key file yet, fall back to the per-process secret WITHOUT caching, so a later
    call picks up the disk-derived secret once the key exists."""
    monkeypatch.setenv("MENHIR_OAUTH_AS_DIR", str(tmp_path))
    monkeypatch.setattr(oauth_authorize, "_PROCESS_CONSENT_SECRET", b"proc" * 8)
    assert oauth_authorize._consent_secret() == b"proc" * 8
    assert oauth_authorize._PERSISTENT_CONSENT_SECRET is None
    key_bytes = _write_key(tmp_path)
    expected = sha256(b"menhir-as-consent-v1\0" + key_bytes).digest()
    assert oauth_authorize._consent_secret() == expected


# ---------------------------------------------------------------------------
# T12 — operator preflight warning
# ---------------------------------------------------------------------------


def _settings(*, as_enabled: bool = False, consent_secret: str = "") -> MemorySettings:
    return MemorySettings(
        api_host="127.0.0.1",
        operator_key="s3cret",
        oauth_as_enabled=as_enabled,
        oauth_public_base_url="http://127.0.0.1:8100" if as_enabled else "",
        oauth_as_consent_secret=consent_secret,
    )


def _find(checks, name):
    return next((c for c in checks if c["name"] == name), None)


def test_preflight_warns_when_consent_secret_unset(monkeypatch):
    monkeypatch.delenv("MENHIR_OAUTH_AS_CONSENT_SECRET", raising=False)
    diag = build_operator_diagnostics(_settings(as_enabled=True))
    check = _find(diag["checks"], "oauth_as_consent_secret")
    assert check is not None and check["status"] == "warn"


def test_preflight_pass_when_consent_secret_set(monkeypatch):
    diag = build_operator_diagnostics(
        _settings(as_enabled=True, consent_secret="shared")
    )
    check = _find(diag["checks"], "oauth_as_consent_secret")
    assert check is not None and check["status"] == "pass"


def test_preflight_check_absent_when_as_disabled(monkeypatch):
    monkeypatch.delenv("MENHIR_OAUTH_AS_ENABLED", raising=False)
    monkeypatch.delenv("MENHIR_OAUTH_AS_CONSENT_SECRET", raising=False)
    diag = build_operator_diagnostics(_settings())
    assert _find(diag["checks"], "oauth_as_consent_secret") is None
