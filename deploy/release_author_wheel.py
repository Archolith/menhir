"""OAuth wheel archive binding against the reviewed source commit."""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import stat
import zipfile
from pathlib import Path

from release_author_git import _git_package_files, _source_tree_digest


def _validate_wheel_record(
    archive: zipfile.ZipFile,
    infos: list[zipfile.ZipInfo],
    record_name: str,
) -> None:
    try:
        rows = list(csv.reader(io.StringIO(archive.read(record_name).decode("utf-8"))))
    except (KeyError, UnicodeDecodeError, csv.Error) as exc:
        raise ValueError("OAuth wheel RECORD is invalid") from exc
    entries: dict[str, tuple[str, str]] = {}
    for row in rows:
        if len(row) != 3 or row[0] in entries:
            raise ValueError("OAuth wheel RECORD is invalid")
        entries[row[0]] = (row[1], row[2])
    names = {item.filename for item in infos}
    if set(entries) != names or entries.get(record_name) != ("", ""):
        raise ValueError("OAuth wheel RECORD does not cover the exact archive")
    for item in infos:
        if item.filename == record_name:
            continue
        payload = archive.read(item)
        digest = base64.urlsafe_b64encode(
            hashlib.sha256(payload).digest()
        ).rstrip(b"=").decode("ascii")
        if entries[item.filename] != (f"sha256={digest}", str(len(payload))):
            raise ValueError(f"OAuth wheel RECORD mismatch: {item.filename}")


def _bind_oauth_wheel_source(repo: Path, commit: str, wheel: Path) -> str:
    source_files = _git_package_files(repo, commit)
    with zipfile.ZipFile(wheel) as archive:
        infos = [item for item in archive.infolist() if not item.is_dir()]
        names = [item.filename for item in infos]
        if len(names) != len(set(names)):
            raise ValueError("OAuth wheel contains duplicate archive members")
        for item in infos:
            parts = item.filename.split("/")
            if (
                item.filename.startswith("/")
                or "\\" in item.filename
                or any(part in {"", ".", ".."} for part in parts)
                or stat.S_ISLNK(item.external_attr >> 16)
            ):
                raise ValueError("OAuth wheel contains an unsafe archive member")
        package_infos = {
            item.filename.removeprefix("archolith_oauth/"): item
            for item in infos if item.filename.startswith("archolith_oauth/")
        }
        metadata_dirs = {
            item.filename.split("/", 1)[0]
            for item in infos
            if "/" in item.filename
            and item.filename.split("/", 1)[0].startswith("archolith_oauth-")
            and item.filename.split("/", 1)[0].endswith(".dist-info")
        }
        if len(metadata_dirs) != 1:
            raise ValueError("OAuth wheel metadata layout is not authorized")
        metadata_dir = metadata_dirs.pop()
        metadata_infos = {
            item.filename.removeprefix(f"{metadata_dir}/"): item
            for item in infos if item.filename.startswith(f"{metadata_dir}/")
        }
        allowed_metadata_names = {
            "METADATA", "WHEEL", "RECORD", "entry_points.txt", "top_level.txt",
            "licenses/LICENSE",
        }
        if (
            not {"METADATA", "WHEEL", "RECORD"} <= set(metadata_infos)
            or not set(metadata_infos) <= allowed_metadata_names
        ):
            raise ValueError("OAuth wheel metadata layout is not authorized")
        _validate_wheel_record(archive, infos, f"{metadata_dir}/RECORD")
        metadata_members = {
            f"{metadata_dir}/{relative}" for relative in metadata_infos
        }
        reviewed_members = {
            item.filename for item in package_infos.values()
        } | metadata_members
        if reviewed_members != set(names):
            raise ValueError("OAuth wheel contains unreviewed installable payload")
        if set(package_infos) != set(source_files):
            raise ValueError("OAuth wheel package tree differs from reviewed OAuth source commit")
        for relative, expected in source_files.items():
            info = package_infos[relative]
            if archive.read(info) != expected:
                raise ValueError(
                    "OAuth wheel payload differs from reviewed OAuth source commit"
                )
    return _source_tree_digest(source_files)

