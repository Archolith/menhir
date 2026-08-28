#!/usr/bin/env python3
"""Fail-closed verification for the production offline wheelhouse."""

from __future__ import annotations

import hashlib
import os
import re
import sys
import zipfile
from email.parser import BytesParser
from pathlib import Path


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MANIFEST_RE = re.compile(r"^([0-9a-f]{64})  ([A-Za-z0-9][A-Za-z0-9_.+-]*\.whl)$")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _regular_file(path: Path, label: str) -> None:
    stat_result = path.lstat()
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"{label} must be a regular non-symlink file")
    if stat_result.st_nlink < 1:
        raise ValueError(f"{label} has an invalid link count")


def _distribution_name(wheel: Path) -> str:
    try:
        with zipfile.ZipFile(wheel) as archive:
            names = [
                name
                for name in archive.namelist()
                if name.endswith(".dist-info/METADATA")
                and name.count("/") == 1
            ]
            if len(names) != 1:
                raise ValueError(f"{wheel.name} must contain exactly one METADATA file")
            metadata = BytesParser().parsebytes(archive.read(names[0]))
    except (OSError, KeyError, zipfile.BadZipFile) as exc:
        raise ValueError(f"invalid wheel archive {wheel.name}") from exc
    name = metadata.get("Name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"{wheel.name} METADATA has no distribution Name")
    return re.sub(r"[-_.]+", "-", name.strip()).lower()


def verify(
    wheelhouse: Path,
    manifest: Path,
    expected_manifest_sha256: str,
    expected_oauth_sha256: str,
) -> None:
    if not SHA256_RE.fullmatch(expected_manifest_sha256):
        raise ValueError("expected manifest digest must be lowercase SHA-256")
    if not SHA256_RE.fullmatch(expected_oauth_sha256):
        raise ValueError("expected OAuth wheel digest must be lowercase SHA-256")
    if not wheelhouse.is_dir() or wheelhouse.is_symlink():
        raise ValueError("wheelhouse must be a regular directory")
    _regular_file(manifest, "wheel manifest")
    if manifest.parent.resolve() != wheelhouse.resolve():
        raise ValueError("wheel manifest must be inside the wheelhouse")
    if _sha256(manifest) != expected_manifest_sha256:
        raise ValueError("wheel manifest digest mismatch")

    raw = manifest.read_text(encoding="ascii")
    if len(raw.encode("ascii")) > 1024 * 1024:
        raise ValueError("wheel manifest is too large")
    entries: dict[str, str] = {}
    for line_number, line in enumerate(raw.splitlines(), start=1):
        match = MANIFEST_RE.fullmatch(line)
        if match is None:
            raise ValueError(f"invalid wheel manifest line {line_number}")
        digest, filename = match.groups()
        if filename in entries:
            raise ValueError(f"duplicate wheel manifest entry: {filename}")
        entries[filename] = digest
    if not entries:
        raise ValueError("wheel manifest is empty")

    actual_names: set[str] = set()
    oauth_digests: list[str] = []
    with os.scandir(wheelhouse) as iterator:
        for entry in iterator:
            if entry.name == manifest.name:
                continue
            if not entry.name.endswith(".whl"):
                raise ValueError(f"unexpected wheelhouse entry: {entry.name}")
            path = wheelhouse / entry.name
            _regular_file(path, f"wheel {entry.name}")
            actual_names.add(entry.name)
            actual_digest = _sha256(path)
            if entries.get(entry.name) != actual_digest:
                raise ValueError(f"wheel digest mismatch: {entry.name}")
            if _distribution_name(path) == "archolith-oauth":
                oauth_digests.append(actual_digest)

    if actual_names != set(entries):
        missing = sorted(set(entries) - actual_names)
        extra = sorted(actual_names - set(entries))
        raise ValueError(f"wheel manifest closure mismatch: missing={missing}, extra={extra}")
    if oauth_digests != [expected_oauth_sha256]:
        raise ValueError("wheelhouse must contain exactly the authorized archolith-oauth wheel")


def main(argv: list[str]) -> int:
    if len(argv) != 5:
        print(
            "usage: verify_wheelhouse.py WHEELHOUSE MANIFEST "
            "MANIFEST_SHA256 OAUTH_WHEEL_SHA256",
            file=sys.stderr,
        )
        return 2
    try:
        verify(Path(argv[1]), Path(argv[2]), argv[3], argv[4])
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"wheelhouse verification failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
