"""Menhir compatibility wrapper for the shared authorization-code store."""

from __future__ import annotations

import threading
from pathlib import Path

from archolith_oauth import (
    AuthCodeRecord,
    AuthorizationCodeStore,
    hash_secret,
    verify_s256,
)

_DEFAULT_TTL_S = 120.0


def hash_code(raw: str) -> str:
    return hash_secret(raw)


def verify_pkce(verifier: str, challenge: str) -> bool:
    return verify_s256(verifier, challenge)


class AuthCodeStore(AuthorizationCodeStore):
    """Retain Menhir's class name and legacy ``_ttl_s`` attribute."""

    def __init__(self, db_path: Path, *, ttl_s: float | None = None) -> None:
        resolved = _DEFAULT_TTL_S if ttl_s is None else float(ttl_s)
        super().__init__(db_path, ttl_s=resolved)
        self._ttl_s = resolved


_auth_code_store_singleton: AuthCodeStore | None = None
_auth_code_store_singleton_lock = threading.Lock()


def configure_auth_code_store(settings: object) -> AuthCodeStore:
    global _auth_code_store_singleton
    from menhir.infrastructure.paths import oauth_as_db_path

    _auth_code_store_singleton = AuthCodeStore(
        oauth_as_db_path(str(getattr(settings, "oauth_as_dir", "")))
        / "menhir_oauth_as.db",
        ttl_s=float(getattr(settings, "oauth_as_code_ttl_s", _DEFAULT_TTL_S)),
    )
    return _auth_code_store_singleton


def get_auth_code_store() -> AuthCodeStore:
    global _auth_code_store_singleton
    if _auth_code_store_singleton is None:
        with _auth_code_store_singleton_lock:
            if _auth_code_store_singleton is None:
                from types import SimpleNamespace

                from menhir.config.oauth import _get_setting

                legacy = object()
                _auth_code_store_singleton = configure_auth_code_store(
                    SimpleNamespace(
                        oauth_as_dir=str(
                            _get_setting(
                                legacy,
                                "oauth_as_dir",
                                "MENHIR_OAUTH_AS_DIR",
                                "",
                            )
                        ),
                        oauth_as_code_ttl_s=float(
                            _get_setting(
                                legacy,
                                "oauth_as_code_ttl_s",
                                "MENHIR_OAUTH_AS_CODE_TTL_S",
                                _DEFAULT_TTL_S,
                            )
                        ),
                    )
                )
    return _auth_code_store_singleton


__all__ = [
    "AuthCodeRecord",
    "AuthCodeStore",
    "configure_auth_code_store",
    "get_auth_code_store",
    "hash_code",
    "verify_pkce",
]
