"""Filesystem and parsing primitives for the personal deployment flow."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


class PersonalDeployError(ValueError):
    """A personal-deployment input, artifact, or transition is invalid."""


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PersonalDeployError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _regular_file(path: Path, label: str) -> Path:
    if not path.is_absolute():
        raise PersonalDeployError(f"{label} must be an absolute path")
    try:
        info = path.lstat()
    except OSError as exc:
        raise PersonalDeployError(f"{label} does not exist: {path}") from exc
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise PersonalDeployError(f"{label} must be a regular non-symlink file")
    return path


def _directory(path: Path, label: str, *, empty: bool = False) -> Path:
    if not path.is_absolute():
        raise PersonalDeployError(f"{label} must be an absolute path")
    try:
        info = path.lstat()
    except OSError as exc:
        raise PersonalDeployError(f"{label} does not exist: {path}") from exc
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise PersonalDeployError(f"{label} must be a non-symlink directory")
    if empty and any(path.iterdir()):
        raise PersonalDeployError(f"{label} must be empty")
    return path.resolve()


def _load_json(path: Path, label: str) -> dict[str, Any]:
    path = _regular_file(path, label)
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_pairs)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PersonalDeployError(f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise PersonalDeployError(f"{label} must be a JSON object")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_sha256(root: Path) -> str:
    root = _directory(root, "install bundle")
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise PersonalDeployError(f"install bundle contains symlink: {relative}")
        if stat.S_ISDIR(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode):
            raise PersonalDeployError(f"install bundle contains special entry: {relative}")
        digest.update(f"{relative}\0{_sha256(path)}\n".encode("utf-8"))
    return digest.hexdigest()


def _composite_sha256(parts: tuple[tuple[str, Path], ...]) -> str:
    digest = hashlib.sha256()
    for label, path in parts:
        digest.update(f"{label}\0{_sha256(path)}\n".encode("ascii"))
    return digest.hexdigest()


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _utc(value: Any, label: str) -> datetime:
    if not isinstance(value, str):
        raise PersonalDeployError(f"{label} must be an explicit UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError as exc:
        raise PersonalDeployError(f"{label} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise PersonalDeployError(f"{label} must be UTC")
    return parsed.astimezone(timezone.utc)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _exact_keys(value: dict[str, Any], expected: frozenset[str], label: str) -> None:
    if set(value) != expected:
        raise PersonalDeployError(f"{label} schema is invalid")
