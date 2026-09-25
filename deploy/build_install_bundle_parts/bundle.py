"""Bundle assembly, census, and manifest validation for the bundle."""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
from pathlib import Path
from typing import Any

_PARTS_PARENT = Path(__file__).resolve().parents[1]
for _candidate in (str(_PARTS_PARENT), str(_PARTS_PARENT / "lib")):
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import menhir_schema  # noqa: E402

from .core import (  # noqa: E402
    INSTALLER_NAME as INSTALLER_NAME,
    MANIFEST_NAME as MANIFEST_NAME,
    RELEASE_DESTINATION as RELEASE_DESTINATION,
    SHA256_RE as SHA256_RE,
)
from .fsio import (  # noqa: E402
    _canonical_destination as _canonical_destination,
    _destination_mode as _destination_mode,
    _directory as _directory,
    _exact_keys as _exact_keys,
    _load_installed_destinations as _load_installed_destinations,
    _sha256_file as _sha256_file,
    _strict_json as _strict_json,
)


def _output_fence(output_path: Path, workspace_root: Path) -> tuple[Path, Path]:
    if not output_path.is_absolute():
        raise ValueError("output must be absolute")
    if output_path.exists() or output_path.is_symlink():
        raise ValueError("output must not already exist")
    root = _directory(str(workspace_root), "workspace root").resolve(strict=True)
    parent = _directory(str(output_path.parent), "output parent").resolve(strict=True)
    try:
        parent.relative_to(root)
    except ValueError as exc:
        raise ValueError("output must be under the caller-provided workspace root") from exc
    if output_path.name in {"", ".", ".."}:
        raise ValueError("output name is invalid")
    return parent / output_path.name, root


def _write_file(path: Path, payload: bytes, mode: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    path.chmod(int(mode, 8))


def _manifest_bytes(manifest: dict[str, Any]) -> bytes:
    return (
        json.dumps(manifest, sort_keys=True, indent=2, ensure_ascii=True) + "\n"
    ).encode("ascii")


def _bundle_census(root: Path) -> tuple[set[str], set[str]]:
    files: set[str] = set()
    directories: set[str] = set()
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise ValueError(f"bundle contains symlink: {relative}")
        if stat.S_ISDIR(info.st_mode):
            directories.add(relative)
        elif stat.S_ISREG(info.st_mode):
            files.add(relative)
        else:
            raise ValueError(f"bundle contains special file: {relative}")
    return files, directories


def _validate_bundle(root: Path, installer_digest: str) -> dict[str, Any]:
    manifest_path = root / MANIFEST_NAME
    installer_path = root / INSTALLER_NAME
    manifest = _strict_json(manifest_path, "bundle manifest")
    _exact_keys(
        manifest,
        frozenset({"schema", "kind", "release_id", "release_sha256", "files"}),
        "bundle manifest",
    )
    if manifest.get("schema") != 1 \
            or manifest.get("kind") != "menhir-release-install-bundle":
        raise ValueError("bundle manifest kind/schema mismatch")
    if not SHA256_RE.fullmatch(str(manifest.get("release_sha256", ""))):
        raise ValueError("bundle manifest release digest is invalid")
    rows = manifest.get("files")
    if not isinstance(rows, dict) or not rows:
        raise ValueError("bundle manifest files must be a non-empty object")
    allowed = _load_installed_destinations() | frozenset({RELEASE_DESTINATION})
    if set(rows) != allowed:
        raise ValueError(
            f"bundle manifest destination census mismatch: "
            f"missing={sorted(allowed - set(rows))}, "
            f"extra={sorted(set(rows) - allowed)}"
        )
    expected_files = {MANIFEST_NAME, INSTALLER_NAME}
    for destination, row in rows.items():
        _canonical_destination(destination, allowed, "bundle manifest destination")
        if not isinstance(row, dict) or set(row) != {"mode", "sha256"}:
            raise ValueError(f"bundle manifest row is invalid: {destination}")
        mode = row.get("mode")
        digest = row.get("sha256")
        if mode != _destination_mode(destination) \
                or not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
            raise ValueError(f"bundle manifest mode/digest is invalid: {destination}")
        relative = "rootfs" + destination
        expected_files.add(relative.lstrip("/"))
        payload_path = root / relative.lstrip("/")
        try:
            info = payload_path.lstat()
        except OSError as exc:
            raise ValueError(f"bundle payload is missing: {destination}") from exc
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise ValueError(f"unsafe bundle payload: {destination}")
        if _sha256_file(payload_path) != digest:
            raise ValueError(f"bundle payload digest mismatch: {destination}")
        if os.name != "nt" and stat.S_IMODE(info.st_mode) != int(mode, 8):
            raise ValueError(f"bundle payload mode mismatch: {destination}")
    files, _ = _bundle_census(root)
    if files != expected_files:
        raise ValueError(
            f"bundle file census mismatch: missing={sorted(expected_files - files)}, "
            f"extra={sorted(files - expected_files)}"
        )
    if _sha256_file(installer_path) != installer_digest:
        raise ValueError("copied installer digest mismatch")
    release_path = root / ("rootfs" + RELEASE_DESTINATION)
    if _sha256_file(release_path) != manifest["release_sha256"]:
        raise ValueError("bundled release digest mismatch")
    release = _strict_json(release_path, "bundled release")
    if release.get("release_id") != manifest.get("release_id"):
        raise ValueError("bundle manifest release id mismatch")
    validated_release = menhir_schema.validate_release(str(release_path))
    release_artifacts = validated_release.get("artifacts")
    installed = allowed - frozenset({RELEASE_DESTINATION})
    if not isinstance(release_artifacts, dict) or set(release_artifacts) != installed:
        raise ValueError("bundled release artifact census mismatch")
    for destination, entry in release_artifacts.items():
        if entry.get("sha256") != rows[destination]["sha256"]:
            raise ValueError(f"bundled release artifact digest mismatch: {destination}")
    return manifest


def _normalize_timestamps(root: Path) -> None:
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        try:
            os.utime(path, (0, 0), follow_symlinks=False)
        except NotImplementedError:
            os.utime(path, (0, 0))
    try:
        os.utime(root, (0, 0), follow_symlinks=False)
    except NotImplementedError:
        os.utime(root, (0, 0))


def _remove_tree(path: Path) -> None:
    """Remove a private build tree, including read-only Windows payloads."""

    for child in sorted(path.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        try:
            child.chmod(0o700 if child.is_dir() else 0o600)
        except OSError:
            pass
    path.chmod(0o700)
    shutil.rmtree(path)
