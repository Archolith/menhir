"""Atomic JSON/artifact writes and validation-bundle read-side helpers."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .base import DIGEST_RE, EVIDENCE_FILES, SHA256_RE, BuildImageError, sha256_bytes


def _write_evidence(path: Path, raw: bytes) -> str:
    if path.parent.is_symlink():
        raise BuildImageError("evidence destination must not traverse a symlink")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        raise BuildImageError(f"evidence destination already exists: {path}")
    with path.open("xb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    return sha256_bytes(raw)


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _read_bytes(path: Path, label: str) -> bytes:
    if not path.is_file() or path.is_symlink():
        raise BuildImageError(f"{label} is missing or unsafe")
    return path.read_bytes()


def _read_json_bytes(raw: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("ascii"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise BuildImageError(f"{label} is not valid ASCII JSON") from exc
    if not isinstance(value, dict):
        raise BuildImageError(f"{label} must contain a JSON object")
    return value


def _require_sha256(value: Any, label: str, *, prefixed: bool = False) -> str:
    pattern = DIGEST_RE if prefixed else SHA256_RE
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise BuildImageError(f"{label} is missing or malformed")
    return value


def _verify_artifact_file(root: Path, relative: str, label: str) -> Path:
    allowed = {
        "release-image.tar",
        "release-image-metadata.json",
        "release-image-identity.json",
        *EVIDENCE_FILES.values(),
    }
    if relative not in allowed:
        raise BuildImageError(f"{label} has an unexpected artifact path")
    current = root
    for part in Path(relative).parts:
        current = current / part
        if current.is_symlink():
            raise BuildImageError(f"{label} must not traverse a symlink")
    try:
        candidate = current.resolve(strict=True)
        candidate.relative_to(root)
    except (FileNotFoundError, ValueError):
        raise BuildImageError(f"{label} escaped the validation artifact") from None
    if not candidate.is_file() or candidate.is_symlink():
        raise BuildImageError(f"{label} is missing or unsafe")
    return candidate
