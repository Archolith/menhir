"""Tests for the embedded OAuth AS registered-client store (Phase 2)."""

from __future__ import annotations

import pytest

from menhir.api.oauth_client_store import (
    OAuthClient,
    OAuthClientStore,
    hash_secret,
    new_client_id,
)

pytestmark = pytest.mark.unit


def _make_client(client_id="c1", secret="s3cret", auth_method="client_secret_post"):
    return OAuthClient(
        client_id=client_id,
        client_name="Test Client",
        redirect_uris=("https://app.example.com/callback",),
        scopes=("menhir:read", "menhir:write"),
        client_secret_hash=hash_secret(secret) if auth_method != "none" else "",
        created_at=1234567890.0,
        token_endpoint_auth_method=auth_method,
    )


def test_register_and_get_roundtrip(tmp_path):
    store = OAuthClientStore(tmp_path / "as.db")
    client = _make_client()
    store.register(client)
    got = store.get("c1")
    assert got is not None
    assert isinstance(got.redirect_uris, tuple)
    assert isinstance(got.scopes, tuple)
    assert got.redirect_uris == ("https://app.example.com/callback",)
    assert got.scopes == ("menhir:read", "menhir:write")
    assert got.client_name == "Test Client"
    assert got.token_endpoint_auth_method == "client_secret_post"
    assert got.created_at == 1234567890.0


def test_persistence_across_instances(tmp_path):
    db = tmp_path / "as.db"
    store1 = OAuthClientStore(db)
    store1.register(_make_client())
    store2 = OAuthClientStore(db)
    got = store2.get("c1")
    assert got is not None
    assert got.client_id == "c1"


def _pub_client(client_id, created_at):
    return OAuthClient(
        client_id=client_id,
        client_name=client_id,
        redirect_uris=("https://app.example.com/cb",),
        scopes=("menhir:read",),
        client_secret_hash="",
        created_at=created_at,
        token_endpoint_auth_method="none",
    )


def test_mark_exchanged_sets_timestamp(tmp_path):
    """AS-002: a completed token exchange stamps last_exchanged."""
    store = OAuthClientStore(tmp_path / "as.db")
    store.register(_pub_client("c", 0.0))
    assert store.get("c").last_exchanged is None
    store.mark_exchanged("c", now=123.0)
    assert store.get("c").last_exchanged == 123.0


def test_reap_stale_removes_only_never_exchanged_old_clients(tmp_path):
    """AS-002: the reaper deletes old, never-exchanged clients; it never touches a
    client that has ever exchanged, nor a recent registration."""
    store = OAuthClientStore(tmp_path / "as.db")
    now = 1_000_000.0
    store.register(_pub_client("old", now - 100_000))     # old, never exchanged -> reaped
    store.register(_pub_client("fresh", now - 10))        # recent, never exchanged -> kept
    store.register(_pub_client("used", now - 100_000))    # old but exchanged -> kept
    store.mark_exchanged("used", now=now - 50_000)

    reaped = store.reap_stale(max_age_s=86400, now=now)

    assert reaped == 1
    assert store.get("old") is None
    assert store.get("fresh") is not None
    assert store.get("used") is not None
    assert store.get("used").last_exchanged == now - 50_000


def test_duplicate_client_id_raises(tmp_path):
    store = OAuthClientStore(tmp_path / "as.db")
    store.register(_make_client())
    with pytest.raises(ValueError):
        store.register(_make_client())


def test_secret_never_stored_plaintext(tmp_path):
    store = OAuthClientStore(tmp_path / "as.db")
    store.register(_make_client(secret="topsecretvalue"))
    raw = (tmp_path / "as.db").read_bytes()
    assert b"topsecretvalue" not in raw
    assert store.verify_secret("c1", "topsecretvalue") is True
    assert store.verify_secret("c1", "wrong") is False


def test_public_client_has_empty_hash(tmp_path):
    store = OAuthClientStore(tmp_path / "as.db")
    store.register(_make_client(client_id="pub", auth_method="none"))
    got = store.get("pub")
    assert got is not None
    assert got.client_secret_hash == ""
    assert store.verify_secret("pub", "anything") is False


def test_verify_secret_unknown_client(tmp_path):
    store = OAuthClientStore(tmp_path / "as.db")
    assert store.verify_secret("nope", "x") is False


def test_all_lists_registered(tmp_path):
    store = OAuthClientStore(tmp_path / "as.db")
    store.register(_make_client(client_id="c1"))
    store.register(_make_client(client_id="c2"))
    clients = store.all()
    assert len(clients) == 2
    assert {c.client_id for c in clients} == {"c1", "c2"}


def test_new_client_id_is_unique_hex():
    cid1 = new_client_id()
    cid2 = new_client_id()
    assert len(cid1) == 16
    assert len(cid2) == 16
    assert int(cid1, 16)
    assert int(cid2, 16)
    assert cid1 != cid2
