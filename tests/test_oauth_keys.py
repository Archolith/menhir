"""Tests for the embedded OAuth AS signing-key bootstrap + JWKS endpoint (Phase 1)."""

from __future__ import annotations

import json
import stat
import sys

import pytest
from joserfc import jwt
from joserfc.jwk import KeySet
from fastapi import FastAPI
from fastapi.testclient import TestClient

from menhir.api import oauth_keys
from menhir.api.oauth_keys import (
    get_signing_key,
    load_or_create_signing_key,
    public_jwks,
)
from menhir.api.oauth_metadata import router as oauth_metadata_router

pytestmark = pytest.mark.unit

_PRIVATE_PARAMS = ("d", "p", "q", "dp", "dq", "qi")


def test_key_persists_across_calls(tmp_path):
    key_path = tmp_path / "oauth_signing_key.json"

    first = load_or_create_signing_key(key_path)
    assert key_path.exists()
    first_pub = first.as_dict(private=False)

    second = load_or_create_signing_key(key_path)
    second_pub = second.as_dict(private=False)

    assert first_pub["kid"] == second_pub["kid"]
    assert first_pub["n"] == second_pub["n"]


def test_kid_is_stable(tmp_path):
    key_path = tmp_path / "oauth_signing_key.json"

    key = load_or_create_signing_key(key_path)
    assert key.as_dict(private=False)["kid"] == key.thumbprint()

    reloaded = load_or_create_signing_key(key_path)
    assert reloaded.as_dict(private=False)["kid"] == key.thumbprint()


def test_public_jwks_has_no_private_material(tmp_path):
    key = load_or_create_signing_key(tmp_path / "oauth_signing_key.json")

    jwks = public_jwks(key)
    assert set(jwks.keys()) == {"keys"}
    assert len(jwks["keys"]) == 1
    for member in jwks["keys"]:
        for param in _PRIVATE_PARAMS:
            assert param not in member
        assert member["kty"] == "RSA"
        assert member["kid"]
        assert member["n"]
        assert member["e"]


def test_sign_verify_round_trip(tmp_path):
    key = load_or_create_signing_key(tmp_path / "oauth_signing_key.json")
    kid = key.as_dict(private=False)["kid"]

    token = jwt.encode({"alg": "RS256", "kid": kid}, {"sub": "menhir-test"}, key)
    key_set = KeySet.import_key_set(public_jwks(key))
    decoded = jwt.decode(token, key_set, algorithms=["RS256"])

    assert decoded.claims["sub"] == "menhir-test"


def test_jwks_endpoint_shape(tmp_path, monkeypatch):
    # Point the singleton at a throwaway dir and reset the cached key.
    monkeypatch.setenv("MENHIR_OAUTH_AS_DIR", str(tmp_path))
    monkeypatch.setattr(oauth_keys, "_SIGNING_KEY", None, raising=False)

    app = FastAPI()
    app.include_router(oauth_metadata_router)
    client = TestClient(app)

    resp = client.get("/.well-known/jwks.json")

    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"keys"}
    assert len(body["keys"]) == 1
    member = body["keys"][0]
    assert member["kty"] == "RSA"
    assert member["kid"]
    assert member["n"]
    assert member["e"]
    for param in _PRIVATE_PARAMS:
        assert param not in member


def test_jwks_endpoint_requires_no_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("MENHIR_OAUTH_AS_DIR", str(tmp_path))
    monkeypatch.setattr(oauth_keys, "_SIGNING_KEY", None, raising=False)

    app = FastAPI()
    app.include_router(oauth_metadata_router)
    client = TestClient(app)

    # No Authorization header supplied.
    resp = client.get("/.well-known/jwks.json")
    assert resp.status_code == 200


def test_get_signing_key_is_cached(tmp_path, monkeypatch):
    monkeypatch.setenv("MENHIR_OAUTH_AS_DIR", str(tmp_path))
    monkeypatch.setattr(oauth_keys, "_SIGNING_KEY", None, raising=False)

    first = get_signing_key()
    second = get_signing_key()
    assert first is second


@pytest.mark.skipif(
    sys.platform.startswith("win"),
    reason="0o600 file mode is not enforceable on Windows",
)
def test_signing_key_file_permissions(tmp_path):
    key_path = tmp_path / "oauth_signing_key.json"
    load_or_create_signing_key(key_path)

    mode = stat.S_IMODE(key_path.stat().st_mode)
    # Not group- or world-readable/writable.
    assert mode & 0o077 == 0

    # Sanity: the persisted file is the private JWK.
    data = json.loads(key_path.read_text())
    assert "d" in data
