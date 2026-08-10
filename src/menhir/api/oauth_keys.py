"""Menhir compatibility wrapper for shared OAuth signing-key management."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from archolith_oauth import SigningKeyStore

_SIGNING_KEY: object | None = None
_SIGNING_KEY_STORE: SigningKeyStore | None = None


def load_or_create_signing_key(key_path: Path):
    """Load or create the active key using Menhir's existing file location."""
    return SigningKeyStore(key_path).load_or_create()


def public_jwks(signing_key) -> dict[str, Any]:
    """Return active plus retained public verification keys when configured.

    The retained (previous-key) set only applies to the configured store's own active
    key. Asked for the JWKS of some other key, honour that key: returning the store's
    keyset instead would advertise a keyset that cannot verify the caller's tokens.
    """
    if _SIGNING_KEY_STORE is not None and signing_key is _SIGNING_KEY:
        return _SIGNING_KEY_STORE.public_jwks_all()
    return SigningKeyStore.public_jwks(signing_key)


def configure_signing_key(settings: object):
    global _SIGNING_KEY, _SIGNING_KEY_STORE
    from menhir.infrastructure.paths import oauth_as_db_path

    key_path = (
        oauth_as_db_path(str(getattr(settings, "oauth_as_dir", "")))
        / "oauth_signing_key.json"
    )
    _SIGNING_KEY_STORE = SigningKeyStore(key_path)
    _SIGNING_KEY = _SIGNING_KEY_STORE.load_or_create()
    return _SIGNING_KEY


def get_signing_key():
    global _SIGNING_KEY
    if _SIGNING_KEY is None:
        from types import SimpleNamespace

        from menhir.config.oauth import _get_setting

        legacy = object()
        _SIGNING_KEY = configure_signing_key(
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
    return _SIGNING_KEY


def get_signing_key_store() -> SigningKeyStore:
    if _SIGNING_KEY_STORE is None:
        get_signing_key()
    assert _SIGNING_KEY_STORE is not None
    return _SIGNING_KEY_STORE


__all__ = [
    "configure_signing_key",
    "get_signing_key",
    "get_signing_key_store",
    "load_or_create_signing_key",
    "public_jwks",
]
