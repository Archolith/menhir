"""Filesystem, JSON, hashing, and destination primitives for the bundle."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path, PurePosixPath
from typing import Any

from .core import (  # noqa: E402
    ALLOWED_GIT_MODES as ALLOWED_GIT_MODES,
    DuplicateKeyError as DuplicateKeyError,
    EVIDENCE_DIGESTS as EVIDENCE_DIGESTS,
    INSTALLER_NAME as INSTALLER_NAME,
    INSTALLER_SOURCE_NAME as INSTALLER_SOURCE_NAME,
    MANIFEST_NAME as MANIFEST_NAME,
    OID_RE as OID_RE,
    PUBLICATION_EVIDENCE as PUBLICATION_EVIDENCE,
    RELEASE_DESTINATION as RELEASE_DESTINATION,
    RENDERED_DESTINATIONS as RENDERED_DESTINATIONS,
    REPOSITORIES as REPOSITORIES,
    SHA256_RE as SHA256_RE,
    SPEC_KEYS as SPEC_KEYS,
    SPEC_KEYS_INHERITED as SPEC_KEYS_INHERITED,
    SPEC_KEYS_WITH_PROVENANCE as SPEC_KEYS_WITH_PROVENANCE,
    STAGING_RUNNER_DESTINATION as STAGING_RUNNER_DESTINATION,
)

# deploy/ directory that holds installed-artifacts.json (same anchor the facade
# derives from its own __file__).
SCRIPT_DIR = Path(__file__).resolve().parents[1]


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise DuplicateKeyError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _strict_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object
        )
    except (OSError, UnicodeError, json.JSONDecodeError, DuplicateKeyError) as exc:
        raise ValueError(f"invalid {label} JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _regular_file(path_value: str | os.PathLike[str], label: str) -> Path:
    if not isinstance(path_value, (str, os.PathLike)) or not os.fspath(path_value):
        raise ValueError(f"{label} must be an absolute file path")
    path = Path(path_value)
    if not path.is_absolute():
        raise ValueError(f"{label} must be absolute")
    try:
        info = path.lstat()
    except OSError as exc:
        raise ValueError(f"{label} does not exist: {path}") from exc
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise ValueError(f"{label} must be an existing regular non-symlink file")
    return path


def _directory(path_value: Any, label: str) -> Path:
    if not isinstance(path_value, str) or not path_value:
        raise ValueError(f"{label} must be an absolute directory path")
    path = Path(path_value)
    if not path.is_absolute():
        raise ValueError(f"{label} must be an absolute directory path")
    try:
        info = path.lstat()
    except OSError as exc:
        raise ValueError(f"{label} does not exist: {path}") from exc
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise ValueError(f"{label} must be an existing non-symlink directory")
    return path


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _exact_keys(value: Any, keys: frozenset[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    if set(value) != keys:
        raise ValueError(
            f"{label} keys mismatch: missing={sorted(keys - set(value))}, "
            f"extra={sorted(set(value) - keys)}"
        )
    return value


def _canonical_source_path(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value \
            or value.startswith("/"):
        raise ValueError(f"{label} must be a canonical repository-relative path")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts) \
            or str(PurePosixPath(value)) != value:
        raise ValueError(f"{label} must be a canonical repository-relative path")
    return value


def _canonical_destination(value: Any, allowed: frozenset[str], label: str) -> str:
    if not isinstance(value, str) or not value.startswith("/") or "\\" in value:
        raise ValueError(f"{label} must be an approved absolute destination")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts[1:]) \
            or str(PurePosixPath(value)) != value or value not in allowed:
        raise ValueError(f"{label} is not an approved canonical destination: {value!r}")
    return value


def _destination_mode(destination: str) -> str:
    if destination == STAGING_RUNNER_DESTINATION:
        return "0755"
    if destination == RELEASE_DESTINATION \
            or destination == "/srv/menhir/production/release/production.env":
        return "0400"
    if destination == "/etc/sudoers.d/menhir-production":
        return "0440"
    if (
        destination.startswith("/srv/menhir/production/bin/")
        and not destination.endswith(".py")
        and destination != "/srv/menhir/production/bin/lib.sh"
    ) or destination == "/usr/local/sbin/menhir-backup-local" \
            or destination.endswith("/check-drift.sh"):
        return "0755"
    return "0644"


def _load_installed_destinations() -> frozenset[str]:
    path = _regular_file(SCRIPT_DIR / "installed-artifacts.json", "installed artifacts")
    value = _strict_json(path, "installed artifacts")
    _exact_keys(value, frozenset({"schema", "destinations"}), "installed artifacts")
    rows = value.get("destinations")
    if value.get("schema") != 1 or not isinstance(rows, list) or not rows:
        raise ValueError("installed artifacts schema is invalid")
    if len(rows) != len(set(rows)):
        raise ValueError("installed artifacts contains duplicate destinations")
    preliminary = frozenset(row for row in rows if isinstance(row, str))
    if len(preliminary) != len(rows):
        raise ValueError("installed artifacts destinations must be strings")
    return frozenset(
        _canonical_destination(row, preliminary, "installed artifact destination")
        for row in rows
    )
