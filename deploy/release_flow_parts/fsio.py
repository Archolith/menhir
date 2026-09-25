"""Filesystem primitives for the release flow: hashing, JSON, and safe trees."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Any

from .core import ReleaseFlowError


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_sha256(root: Path) -> str:
    try:
        root_info = root.lstat()
    except OSError as exc:
        raise ReleaseFlowError(f"bundle does not exist: {root}") from exc
    if not stat.S_ISDIR(root_info.st_mode) or stat.S_ISLNK(root_info.st_mode):
        raise ReleaseFlowError("install bundle must be a non-symlink directory")
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise ReleaseFlowError(f"install bundle contains symlink: {relative}")
        if stat.S_ISDIR(info.st_mode):
            continue
        elif stat.S_ISREG(info.st_mode):
            payload_digest = _sha256(path)
        else:
            raise ReleaseFlowError(f"install bundle contains special entry: {relative}")
        record = f"{relative}\0{payload_digest}\n"
        digest.update(record.encode("utf-8"))
    return digest.hexdigest()


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ReleaseFlowError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _load_json(path: Path, label: str) -> dict[str, Any]:
    path = _regular_file(path, label)
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_pairs)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseFlowError(f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ReleaseFlowError(f"{label} must be a JSON object")
    return value


def _regular_file(path: Path, label: str) -> Path:
    if not path.is_absolute():
        raise ReleaseFlowError(f"{label} must be an absolute path")
    try:
        path.lstat()
    except OSError as exc:
        raise ReleaseFlowError(f"{label} does not exist: {path}") from exc
    if not path.is_file() or path.is_symlink():
        raise ReleaseFlowError(f"{label} must be a regular non-symlink file")
    return path


def _workspace(path: Path, *, create: bool = False) -> Path:
    if not path.is_absolute():
        raise ReleaseFlowError("workspace must be an absolute path")
    if create:
        path.mkdir(parents=False, exist_ok=False)
    try:
        path.lstat()
    except OSError as exc:
        raise ReleaseFlowError(f"workspace does not exist: {path}") from exc
    if not path.is_dir() or path.is_symlink():
        raise ReleaseFlowError("workspace must be a non-symlink directory")
    return path.resolve()


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(_json_text(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _json_text(value: dict[str, Any]) -> str:
    return json.dumps(value, indent=2, sort_keys=True) + "\n"


def _json_sha256(value: dict[str, Any]) -> str:
    return hashlib.sha256(_json_text(value).encode("utf-8")).hexdigest()


def _atomic_text(path: Path, value: str) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _remove_private_tree(path: Path) -> None:
    """Remove a coordinator-owned tree, including read-only Windows files."""
    if path.is_symlink() or not path.is_dir():
        raise ReleaseFlowError(f"managed release path is unsafe: {path.name}")
    for child in sorted(path.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if child.is_symlink():
            raise ReleaseFlowError(f"managed release tree contains a symlink: {path.name}")
        try:
            child.chmod(0o700 if child.is_dir() else 0o600)
        except OSError:
            pass
    try:
        path.chmod(0o700)
    except OSError:
        pass
    shutil.rmtree(path)


def _remove_managed_path(workspace: Path, path: Path) -> None:
    if path.parent != workspace:
        raise ReleaseFlowError("managed release path escaped its workspace")
    try:
        path.lstat()
    except FileNotFoundError:
        return
    if path.is_symlink():
        raise ReleaseFlowError(f"managed release path is a symlink: {path.name}")
    if path.is_dir():
        _remove_private_tree(path)
        return
    if not path.is_file():
        raise ReleaseFlowError(f"managed release path is unsafe: {path.name}")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    path.unlink()


def _regular_directory(path: Path, label: str) -> Path:
    if not path.is_absolute():
        raise ReleaseFlowError(f"{label} must be an absolute path")
    try:
        path.lstat()
    except OSError as exc:
        raise ReleaseFlowError(f"{label} does not exist: {path}") from exc
    if not path.is_dir() or path.is_symlink():
        raise ReleaseFlowError(f"{label} must be a non-symlink directory")
    return path
