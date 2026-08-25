"""Menhir compatibility wrapper for the shared refresh-token store."""

from __future__ import annotations

import threading

from archolith_oauth import RefreshTokenStore

_DEFAULT_TTL_S = 2592000.0

_refresh_store_singleton: RefreshTokenStore | None = None
_refresh_store_singleton_lock = threading.Lock()


def configure_refresh_store(settings: object) -> RefreshTokenStore:
    global _refresh_store_singleton
    from menhir.infrastructure.paths import oauth_as_db_path

    _refresh_store_singleton = RefreshTokenStore(
        oauth_as_db_path(str(getattr(settings, "oauth_as_dir", "")))
        / "menhir_oauth_as.db",
        ttl_s=float(getattr(settings, "oauth_as_refresh_ttl_s", _DEFAULT_TTL_S)),
    )
    return _refresh_store_singleton


def get_refresh_store() -> RefreshTokenStore:
    global _refresh_store_singleton
    if _refresh_store_singleton is None:
        with _refresh_store_singleton_lock:
            if _refresh_store_singleton is None:
                from types import SimpleNamespace

                from menhir.config.oauth import _get_setting

                legacy = object()
                _refresh_store_singleton = configure_refresh_store(
                    SimpleNamespace(
                        oauth_as_dir=str(
                            _get_setting(
                                legacy,
                                "oauth_as_dir",
                                "MENHIR_OAUTH_AS_DIR",
                                "",
                            )
                        ),
                        oauth_as_refresh_ttl_s=float(
                            _get_setting(
                                legacy,
                                "oauth_as_refresh_ttl_s",
                                "MENHIR_OAUTH_AS_REFRESH_TTL_S",
                                _DEFAULT_TTL_S,
                            )
                        ),
                    )
                )
    return _refresh_store_singleton


__all__ = [
    "RefreshTokenStore",
    "configure_refresh_store",
    "get_refresh_store",
]
