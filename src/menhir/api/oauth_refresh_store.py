"""Menhir compatibility wrapper for the shared refresh-token store."""

from __future__ import annotations

import base64
import binascii
import json
import re
import threading
from pathlib import Path

from archolith_oauth import ReceiptEncryptionKeyring, RefreshTokenStore

_DEFAULT_TTL_S = 2592000.0
_KEYRING_MAX_BYTES = 64 * 1024
_KEYRING_MAX_KEYS = 8
_KEY_ID_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,64}")

_refresh_store_singleton: RefreshTokenStore | None = None
_refresh_keyring_singleton: ReceiptEncryptionKeyring | None = None
_refresh_store_singleton_lock = threading.Lock()


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"refresh retry keyring contains duplicate key {key!r}")
        result[key] = value
    return result


def _load_retry_keyring(path_value: str) -> ReceiptEncryptionKeyring:
    path = Path(path_value)
    if not path.is_file():
        raise ValueError("refresh retry keyring path must name a regular file")
    if path.stat().st_size > _KEYRING_MAX_BYTES:
        raise ValueError("refresh retry keyring exceeds the 64 KiB operator bound")
    try:
        payload = json.loads(
            path.read_text("utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("refresh retry keyring is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise ValueError("refresh retry keyring must be a version-1 JSON object")
    if set(payload) != {"version", "current_key_id", "keys"}:
        raise ValueError("refresh retry keyring contains unexpected fields")
    keys = payload.get("keys")
    current_key_id = payload.get("current_key_id")
    if (
        not isinstance(keys, dict)
        or not keys
        or len(keys) > _KEYRING_MAX_KEYS
        or not isinstance(current_key_id, str)
    ):
        raise ValueError("refresh retry keyring keys/current_key_id are invalid")
    decoded: dict[str, bytes] = {}
    for key_id, encoded in keys.items():
        if (
            not isinstance(key_id, str)
            or _KEY_ID_PATTERN.fullmatch(key_id) is None
            or not isinstance(encoded, str)
            or not encoded
            or "=" in encoded
        ):
            raise ValueError("refresh retry keyring entries are invalid")
        try:
            raw = base64.b64decode(
                encoded + "=" * (-len(encoded) % 4),
                altchars=b"-_",
                validate=True,
            )
        except (ValueError, binascii.Error) as exc:
            raise ValueError("refresh retry keyring contains invalid base64url") from exc
        if base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii") != encoded:
            raise ValueError("refresh retry keyring contains non-canonical base64url")
        decoded[key_id] = raw
    return ReceiptEncryptionKeyring(decoded, current_key_id=current_key_id)


def configure_refresh_store(settings: object) -> RefreshTokenStore:
    global _refresh_keyring_singleton, _refresh_store_singleton
    from menhir.infrastructure.paths import oauth_as_db_path

    grace_s = float(getattr(settings, "oauth_as_refresh_retry_grace_s", 0.0))
    keyring_path = str(
        getattr(settings, "oauth_refresh_retry_keyring_path", "")
    ).strip()
    if grace_s > 0:
        if not keyring_path:
            raise ValueError(
                "MENHIR_OAUTH_REFRESH_RETRY_KEYRING_PATH is required when durable retry grace is enabled"
            )
        _refresh_keyring_singleton = _load_retry_keyring(keyring_path)
    else:
        _refresh_keyring_singleton = None
    _refresh_store_singleton = RefreshTokenStore(
        oauth_as_db_path(str(getattr(settings, "oauth_as_dir", "")))
        / "menhir_oauth_as.db",
        ttl_s=float(getattr(settings, "oauth_as_refresh_ttl_s", _DEFAULT_TTL_S)),
        receipt_ttl_s=max(grace_s, 0.001),
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


def get_refresh_keyring() -> ReceiptEncryptionKeyring:
    if _refresh_keyring_singleton is None:
        raise RuntimeError("durable refresh retry keyring is not configured")
    return _refresh_keyring_singleton


__all__ = [
    "RefreshTokenStore",
    "configure_refresh_store",
    "get_refresh_store",
    "get_refresh_keyring",
]
