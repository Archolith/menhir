"""Strict regular-file reads, canonical base64url decoding, and strict JSON parsing."""

from __future__ import annotations

import base64
import json
import os
import stat
from pathlib import Path

from .base import _B64URL_RE, SourceFenceError


def _inspect_regular(path: Path, label: str, maximum: int) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as exc:
        raise SourceFenceError(f"cannot inspect {label}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise SourceFenceError(f"{label} must be a regular non-symlink file")
    if info.st_size <= 0 or info.st_size > maximum:
        raise SourceFenceError(f"{label} has an invalid size")
    return info


def _read_regular(path: Path, label: str, maximum: int) -> bytes:
    _inspect_regular(path, label, maximum)
    flags = os.O_RDONLY
    # Windows CRT text mode translates CRLF to LF. These bytes are security
    # authority: release digests and one-line token framing must remain exact.
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise SourceFenceError(f"cannot open {label}") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size <= 0 or info.st_size > maximum:
            raise SourceFenceError(f"{label} has an invalid size or type")
        chunks = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if not raw or len(raw) > maximum:
            raise SourceFenceError(f"{label} has an invalid size")
        return raw
    finally:
        os.close(descriptor)


def _require_root_owned_nonwritable(path: Path, label: str) -> None:
    """Match Menhir's root-owned, non-group/other-writable authority rule."""
    if os.name != "posix":
        return
    info = path.lstat()
    if info.st_uid != 0:
        raise SourceFenceError(f"{label} must be root-owned")
    if stat.S_IMODE(info.st_mode) & 0o022:
        raise SourceFenceError(f"{label} must not be group/other writable")


def _strict_json_bytes(raw: bytes, label: str):
    def reject_duplicates(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate key: {key}")
            result[key] = value
        return result

    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=reject_duplicates)
    except (UnicodeDecodeError, ValueError) as exc:
        raise SourceFenceError(f"invalid {label}") from exc


def _decode_b64url(value: object, label: str, length: int) -> bytes:
    if not isinstance(value, str) or not value or "=" in value or not _B64URL_RE.fullmatch(value):
        raise SourceFenceError(f"{label} is not canonical unpadded base64url")
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, TypeError) as exc:
        raise SourceFenceError(f"{label} is invalid") from exc
    if (
        len(raw) != length
        or base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii") != value
    ):
        raise SourceFenceError(f"{label} has an invalid encoding or length")
    return raw
