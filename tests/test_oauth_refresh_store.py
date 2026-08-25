"""Unit coverage for the refresh-token store wrapper (Phase 6)."""

from __future__ import annotations

from pathlib import Path

import pytest

from menhir.api.oauth_refresh_store import (
    RefreshTokenStore,
    configure_refresh_store,
    get_refresh_store,
)
from menhir.config import MemorySettings
from menhir.infrastructure.paths import oauth_as_db_path

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _reset_singleton():
    import menhir.api.oauth_refresh_store as module

    module._refresh_store_singleton = None
    yield
    module._refresh_store_singleton = None


def test_configure_refresh_store_uses_shared_as_database(tmp_path):
    settings = MemorySettings(oauth_as_dir=str(tmp_path))

    store = configure_refresh_store(settings)

    expected_db = oauth_as_db_path(str(tmp_path)) / "menhir_oauth_as.db"
    assert Path(store.db_path) == expected_db


def test_configured_tokens_survive_complete_store_reconstruction(tmp_path):
    settings = MemorySettings(oauth_as_dir=str(tmp_path))
    store = configure_refresh_store(settings)

    raw = store.issue(
        client_id="client-a",
        subject="user-1",
        scope="menhir:read",
        resource="https://memory.example.com/mcp-http",
    )

    reconstructed = RefreshTokenStore(
        oauth_as_db_path(str(tmp_path)) / "menhir_oauth_as.db",
        ttl_s=2592000.0,
    )
    record, replacement = reconstructed.rotate(
        token=raw,
        client_id="client-a",
        resource="https://memory.example.com/mcp-http",
    )

    assert record is not None
    assert record.subject == "user-1"
    assert replacement


def test_get_refresh_store_returns_the_configured_process_snapshot(tmp_path):
    settings = MemorySettings(oauth_as_dir=str(tmp_path))

    configured = configure_refresh_store(settings)

    assert get_refresh_store() is configured


def test_ttl_is_taken_from_settings(tmp_path):
    settings = MemorySettings(
        oauth_as_dir=str(tmp_path),
        oauth_as_refresh_ttl_s=60,
    )

    store = configure_refresh_store(settings)

    assert store.ttl_s == 60.0
