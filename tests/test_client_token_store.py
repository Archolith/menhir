"""Tests for the hashed client-token store (storage layer only)."""

from __future__ import annotations

from pathlib import Path

import pytest

from menhir.api.client_token_store import (
    ClientTokenRecord,
    ClientTokenStore,
    client_tokens_enabled,
    hash_token,
    new_client_id,
)
from menhir.config import MemorySettings


@pytest.mark.unit
@pytest.mark.parametrize("raw,expected", [("1", True), ("on", False), ("true", True), ("0", False)])
def test_client_tokens_enabled_agrees_with_memory_settings(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: bool
) -> None:
    """Regression (SSOT-07): this module's client_tokens_enabled() must never
    disagree with MemorySettings.client_tokens_enabled for the same env value.
    Previously "on" produced True here (this module's own ad hoc parser) but
    False via MemorySettings -- now both delegate to the one canonical parser."""
    monkeypatch.setenv("MENHIR_CLIENT_TOKENS_ENABLED", raw)
    assert client_tokens_enabled() is expected
    assert client_tokens_enabled() is MemorySettings.from_env().client_tokens_enabled


def test_mint_then_resolve_roundtrip(tmp_path: Path) -> None:
    db = tmp_path / "tokens.db"
    store = ClientTokenStore(db)

    raw, record = store.mint("alpha", "standard")

    assert isinstance(raw, str) and len(raw) > 0
    assert isinstance(record, ClientTokenRecord)
    assert record.client_name == "alpha"
    assert record.tier == "standard"
    assert not record.revoked

    resolved = store.resolve(raw)
    assert resolved is not None
    assert resolved.client_id == record.client_id
    assert resolved.client_name == "alpha"
    assert resolved.tier == "standard"
    assert not resolved.revoked


def test_resolve_unknown_returns_none(tmp_path: Path) -> None:
    db = tmp_path / "tokens.db"
    store = ClientTokenStore(db)

    result = store.resolve("some-random-garbage-token")
    assert result is None


def test_revoke_then_resolve_returns_none(tmp_path: Path) -> None:
    db = tmp_path / "tokens.db"
    store = ClientTokenStore(db)

    raw, record = store.mint("beta", "admin")

    assert store.revoke(record.client_id) is True

    resolved = store.resolve(raw)
    assert resolved is None


def test_tokens_stored_hashed_only(tmp_path: Path) -> None:
    db = tmp_path / "tokens.db"
    store = ClientTokenStore(db)

    raw, record = store.mint("gamma", "standard")

    # Read the raw bytes of the database file
    db_bytes = db.read_bytes()

    # The raw token string must NOT appear anywhere in the db
    assert raw.encode("utf-8") not in db_bytes, (
        "Raw token must not be persisted in the database"
    )

    # The sha256 hash MUST appear in the db
    h = hash_token(raw)
    assert h.encode("utf-8") in db_bytes, (
        "Token hash must be present in the database"
    )


def test_two_clients_distinct_ids(tmp_path: Path) -> None:
    db = tmp_path / "tokens.db"
    store = ClientTokenStore(db)

    _, rec1 = store.mint("client-a", "standard")
    _, rec2 = store.mint("client-b", "standard")

    assert rec1.client_id != rec2.client_id


def test_persistence_across_instances(tmp_path: Path) -> None:
    db = tmp_path / "tokens.db"

    # First instance mints a token
    store1 = ClientTokenStore(db)
    raw, record1 = store1.mint("persist", "gold")

    # Second instance (fresh object, same db file) resolves it
    store2 = ClientTokenStore(db)
    resolved = store2.resolve(raw)

    assert resolved is not None
    assert resolved.client_id == record1.client_id
    assert resolved.client_name == "persist"
    assert resolved.tier == "gold"


def test_has_active_reflects_active_tokens(tmp_path: Path) -> None:
    store = ClientTokenStore(tmp_path / "tokens.db")
    assert store.has_active() is False  # empty
    _raw, rec = store.mint("alpha", "operator")
    assert store.has_active() is True   # one active
    store.revoke(rec.client_id)
    assert store.has_active() is False  # revoked -> none active again


def test_mint_bootstrap_only_when_store_empty(tmp_path: Path) -> None:
    """CT-003: bootstrap mint is atomic on the empty-store precondition, so two
    concurrent bootstraps cannot both mint."""
    store = ClientTokenStore(tmp_path / "tokens.db")

    # Empty store -> bootstrap mint succeeds and is resolvable.
    first = store.mint_bootstrap("bootstrap", "operator")
    assert first is not None
    raw, record = first
    assert store.resolve(raw) is not None

    # An active token now exists -> a second bootstrap mint is refused (None).
    assert store.mint_bootstrap("second", "operator") is None

    # Revoking the last active token re-opens bootstrap.
    assert store.revoke(record.client_id) is True
    again = store.mint_bootstrap("reopened", "operator")
    assert again is not None
