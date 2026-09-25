"""Shared primitives for the Menhir host scaffold: errors, IO, time, and receipts."""

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

from menhir_scaffold_constants import HEX64


class ScaffoldError(RuntimeError):
    """Raised when the host cannot prove the scaffold contract."""


def strict_load(path: Path) -> dict[str, Any]:
    def hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ScaffoldError(f"duplicate JSON key in {path}: {key}")
            value[key] = item
        return value

    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=hook)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ScaffoldError(f"cannot read valid JSON from {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ScaffoldError(f"JSON root must be an object: {path}")
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).isoformat()


def parse_time(value: Any, label: str) -> dt.datetime:
    if not isinstance(value, str):
        raise ScaffoldError(f"{label} timestamp is missing")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ScaffoldError(f"{label} timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise ScaffoldError(f"{label} timestamp lacks a timezone")
    return parsed.astimezone(dt.timezone.utc)


def require_root() -> None:
    if os.geteuid() != 0:
        raise ScaffoldError("this command must run as root")


def require_safe_root_file(path: Path, label: str) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ScaffoldError(f"{label} is missing: {path}") from exc
    if not stat.S_ISREG(info.st_mode) or path.is_symlink():
        raise ScaffoldError(f"{label} must be a regular non-symlink file: {path}")
    if info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o022:
        raise ScaffoldError(f"{label} must be root-owned and not group/other writable")


def run(command: list[str], *, check: bool = True) -> str:
    try:
        result = subprocess.run(
            command, check=check, capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ScaffoldError(f"command failed: {' '.join(command)}: {exc}") from exc
    return result.stdout.strip()


def atomic_json(path: Path, value: dict[str, Any], mode: int = 0o400) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
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


def stat_row(path: str, include_digest: bool, expected_type: str) -> dict[str, Any]:
    target = Path(path)
    try:
        info = target.lstat()
    except OSError as exc:
        raise ScaffoldError(f"required scaffold path is missing: {path}") from exc
    if target.is_symlink():
        raise ScaffoldError(f"scaffold path must not be a symlink: {path}")
    if expected_type == "directory" and not stat.S_ISDIR(info.st_mode):
        raise ScaffoldError(f"scaffold directory is not a directory: {path}")
    if expected_type == "file" and not stat.S_ISREG(info.st_mode):
        raise ScaffoldError(f"scaffold file is not a regular file: {path}")
    row: dict[str, Any] = {
        "path": path,
        "uid": info.st_uid,
        "gid": info.st_gid,
        "mode": f"{stat.S_IMODE(info.st_mode):04o}",
    }
    if include_digest:
        if not stat.S_ISREG(info.st_mode):
            raise ScaffoldError(f"digest-bound scaffold path is not a file: {path}")
        row["sha256"] = sha256_file(target)
    return row


def read_os_release() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value.strip().strip('"')
    return {"os_id": values.get("ID", ""), "os_version": values.get("VERSION_ID", "")}


def age_hours(value: dt.datetime, now: dt.datetime) -> float:
    seconds = (now - value).total_seconds()
    if seconds < -60:
        raise ScaffoldError("evidence timestamp is in the future")
    return max(0.0, seconds / 3600)


def build_receipt(
    contract_path: Path,
    verifier_path: Path,
    observed: dict[str, Any],
    machine_id_sha256: str,
    captured: dt.datetime,
) -> dict[str, Any]:
    if not HEX64.fullmatch(machine_id_sha256):
        raise ScaffoldError("machine-id digest is malformed")
    return {
        "schema": 1,
        "kind": "menhir-host-scaffold-receipt",
        "contract_sha256": sha256_file(contract_path),
        "verifier_sha256": sha256_file(verifier_path),
        "machine_id_sha256": machine_id_sha256,
        "captured_utc": iso(captured),
        "static": observed,
    }


def machine_id_digest() -> str:
    return sha256_file(Path("/etc/machine-id"))
