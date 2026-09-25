"""Filesystem and JSON primitives for release specification loading."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from release_spec_errors import ReleaseSpecError


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ReleaseSpecError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_json(path: Path, label: str) -> dict[str, Any]:
    path = _regular(path, label)
    return _load_json_bytes(path.read_bytes(), label)


def _load_json_bytes(data: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            data.decode("utf-8"), object_pairs_hook=_unique_pairs
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseSpecError(f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ReleaseSpecError(f"{label} must be a JSON object")
    return value


def _exact(value: Any, keys: frozenset[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ReleaseSpecError(f"{label} must be an object")
    missing = sorted(keys - set(value))
    extra = sorted(set(value) - keys)
    if missing or extra:
        raise ReleaseSpecError(
            f"{label} keys mismatch: missing={missing}, extra={extra}"
        )
    return value


def _absolute(path_value: Any, label: str) -> Path:
    if not isinstance(path_value, str) or not path_value:
        raise ReleaseSpecError(f"{label} must be a path string")
    path = Path(path_value)
    if not path.is_absolute():
        raise ReleaseSpecError(f"{label} must be absolute")
    return path


def _regular(path_value: Any, label: str) -> Path:
    path = path_value if isinstance(path_value, Path) else _absolute(path_value, label)
    try:
        resolved = path.resolve(strict=True)
        path.lstat()
    except OSError as exc:
        raise ReleaseSpecError(f"{label} does not exist: {path}") from exc
    if path.is_symlink() or not path.is_file() or (
        os.path.normcase(str(path)) != os.path.normcase(str(resolved))
    ):
        raise ReleaseSpecError(f"{label} must be a regular non-symlink file")
    return resolved


def _directory(path_value: Any, label: str) -> Path:
    path = path_value if isinstance(path_value, Path) else _absolute(path_value, label)
    try:
        resolved = path.resolve(strict=True)
        path.lstat()
    except OSError as exc:
        raise ReleaseSpecError(f"{label} does not exist: {path}") from exc
    if path.is_symlink() or not path.is_dir() or (
        os.path.normcase(str(path)) != os.path.normcase(str(resolved))
    ):
        raise ReleaseSpecError(f"{label} must be an absolute non-symlink directory")
    return resolved


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _capture_bytes(path: Path, label: str) -> bytes:
    return _regular(path, label).read_bytes()
