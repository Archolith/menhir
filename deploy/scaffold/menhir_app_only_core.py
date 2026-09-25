"""Shared primitives for the app-only runner: errors, IO, hashing, and locks."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from menhir_app_only_constants import APP_ONLY_SOURCE_PATTERNS, ENV_KEY


class AppOnlyError(RuntimeError):
    pass


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def is_app_only_source_path(path: str) -> bool:
    """Return whether a source path is proven safe for the no-backup lane."""
    return any(pattern.fullmatch(path) for pattern in APP_ONLY_SOURCE_PATTERNS)


def strict_load(path: Path) -> dict[str, Any]:
    def hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise AppOnlyError(f"duplicate JSON key in {path}: {key}")
            result[key] = value
        return result

    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=hook)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AppOnlyError(f"cannot read valid JSON from {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise AppOnlyError(f"JSON root must be an object: {path}")
    return value


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def composite_sha256(parts: tuple[tuple[str, Path], ...]) -> str:
    """Bind multiple imported executables into one reviewable authority digest."""
    digest = hashlib.sha256()
    for label, path in parts:
        digest.update(f"{label}\0{sha256(path)}\n".encode("ascii"))
    return digest.hexdigest()


def parse_utc(value: object, label: str) -> dt.datetime:
    if not isinstance(value, str):
        raise AppOnlyError(f"{label} must be an explicit UTC timestamp")
    try:
        parsed = dt.datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError as exc:
        raise AppOnlyError(f"{label} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != dt.timedelta(0):
        raise AppOnlyError(f"{label} must be UTC")
    return parsed.astimezone(dt.timezone.utc)


def require_root_file(path: Path, label: str) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise AppOnlyError(f"{label} is missing: {path}") from exc
    if path.is_symlink() or not stat.S_ISREG(info.st_mode):
        raise AppOnlyError(f"{label} must be a regular non-symlink file")
    if info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o022:
        raise AppOnlyError(f"{label} must be root-owned and not group/other writable")


def require_upload(path: Path, label: str) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise AppOnlyError(f"{label} is missing") from exc
    if path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_uid != 1000:
        raise AppOnlyError(f"{label} must be a regular non-symlink file owned by thron")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise AppOnlyError(f"{label} must have mode 0600 or stricter")


def run(command: list[str], timeout: int, *, input_bytes: bytes | None = None) -> str:
    try:
        result = subprocess.run(
            command, input=input_bytes, capture_output=True, check=False, timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise AppOnlyError(f"command failed: {command[0]}: {exc}") from exc
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", "replace")[-1000:].strip()
        raise AppOnlyError(f"command exited {result.returncode}: {' '.join(command)}: {stderr}")
    return result.stdout.decode("utf-8", "replace").strip()


def atomic_bytes(path: Path, data: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.chown(temporary, 0, 0)
        os.replace(temporary, path)
        parent = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    atomic_bytes(
        path,
        (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("ascii"),
        0o400,
    )


def parse_env(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise AppOnlyError(f"cannot read production environment: {exc}") from exc
    for number, line in enumerate(lines, 1):
        if not line or line.startswith("#") or "=" not in line:
            raise AppOnlyError(f"production environment line {number} is not KEY=VALUE")
        key, value = line.split("=", 1)
        if not ENV_KEY.fullmatch(key) or key in result:
            raise AppOnlyError(f"production environment key is invalid or duplicated: {key}")
        if not value or any(char in value for char in "\r\n\x00"):
            raise AppOnlyError(f"production environment value is invalid: {key}")
        result[key] = value
    return result


def set_path(value: dict[str, Any], path: tuple[str, ...], replacement: Any) -> None:
    target: Any = value
    for key in path[:-1]:
        if not isinstance(target, dict) or key not in target:
            raise AppOnlyError("release is missing required field: " + "/".join(path))
        target = target[key]
    if not isinstance(target, dict) or path[-1] not in target:
        raise AppOnlyError("release is missing required field: " + "/".join(path))
    target[path[-1]] = replacement


def json_differences(left: Any, right: Any, prefix: str = "") -> list[str]:
    if type(left) is not type(right):
        return [prefix or "/"]
    if isinstance(left, dict):
        differences: list[str] = []
        for key in sorted(set(left) | set(right)):
            path = f"{prefix}/{key}"
            if key not in left or key not in right:
                differences.append(path)
            else:
                differences.extend(json_differences(left[key], right[key], path))
        return differences
    if isinstance(left, list):
        return [] if left == right else [prefix or "/"]
    return [] if left == right else [prefix or "/"]


class DeploymentLocks:
    """Hold cross-lane admission before the legacy mutation lock."""

    def __init__(self, handles: list[Any]) -> None:
        self._handles = handles

    def close(self) -> None:
        for handle in reversed(self._handles):
            handle.close()
