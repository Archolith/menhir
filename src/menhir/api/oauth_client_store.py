"""Menhir compatibility wrapper for the shared OAuth client store."""

from __future__ import annotations

import secrets
import threading

from archolith_oauth import OAuthClient, OAuthClientStore, hash_secret


def new_client_id() -> str:
    """Preserve Menhir's existing compact DCR client-id format."""
    return secrets.token_hex(8)


_client_store_singleton: OAuthClientStore | None = None
_client_store_singleton_lock = threading.Lock()


def configure_client_store(settings: object) -> OAuthClientStore:
    """Build the process store from the server's immutable settings snapshot."""
    global _client_store_singleton
    from menhir.infrastructure.paths import oauth_as_db_path

    _client_store_singleton = OAuthClientStore(
        oauth_as_db_path(str(getattr(settings, "oauth_as_dir", "")))
        / "menhir_oauth_as.db"
    )
    return _client_store_singleton


def get_client_store() -> OAuthClientStore:
    """Return the shared embedded-AS registered-client store."""
    global _client_store_singleton
    if _client_store_singleton is None:
        with _client_store_singleton_lock:
            if _client_store_singleton is None:
                from types import SimpleNamespace

                from menhir.config.oauth import _get_setting

                legacy = object()
                _client_store_singleton = configure_client_store(
                    SimpleNamespace(
                        oauth_as_dir=str(
                            _get_setting(
                                legacy,
                                "oauth_as_dir",
                                "MENHIR_OAUTH_AS_DIR",
                                "",
                            )
                        )
                    )
                )
    return _client_store_singleton


__all__ = [
    "OAuthClient",
    "OAuthClientStore",
    "configure_client_store",
    "get_client_store",
    "hash_secret",
    "new_client_id",
]
