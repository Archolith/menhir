"""Uploaded security-config bundle validation, digest binding, and staging."""

from __future__ import annotations

import copy
import json
import os
import stat
from pathlib import Path
from typing import Any

import menhir_app_only as app

from menhir_security_config_constants import BUNDLE_ID, DESTINATIONS, Error, TARGETS, UPLOAD_ROOT


def require_candidate_file(path: Path, label: str) -> None:
    app.require_upload(path, label)
    if path.stat().st_size > 4 * 1024 * 1024:
        raise Error(f"{label} is unexpectedly large")


def validate_policy_files(bundle: Path, release: dict[str, Any]) -> None:
    client = app.strict_load(bundle / "client-policy.json")
    if client.get("version") not in {1, 2}:
        raise Error("client-policy.json schema mismatch")
    canonical = copy.deepcopy(client)
    declared = canonical.pop("canonical_digest", None)
    calculated = app.hashlib.sha256(json.dumps(
        canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("ascii")).hexdigest()
    if declared != calculated:
        raise Error("client-policy.json canonical digest mismatch")
    expected = {
        "client-policy.json": release.get("rendered", {}).get("policy_sha256"),
        "production.env": release.get("rendered", {}).get("production_env_sha256"),
    }
    for name, digest in expected.items():
        if digest != app.sha256(bundle / name):
            raise Error(f"candidate {name} digest is not release-bound")


def validate_source_manifest(
    source: dict[str, Any], bundle: Path, release_sha: str, release_id: str,
) -> None:
    if set(source) != {"schema", "kind", "release_id", "release_sha256", "files"} \
            or source.get("schema") != 1 \
            or source.get("kind") != "menhir-release-install-bundle" \
            or source.get("release_id") != release_id \
            or source.get("release_sha256") != release_sha:
        raise Error("source install-bundle manifest binding mismatch")
    files = source.get("files")
    if not isinstance(files, dict):
        raise Error("source install-bundle files are missing")
    for name, destination in DESTINATIONS.items():
        row = files.get(destination)
        if not isinstance(row, dict) or row.get("sha256") != app.sha256(bundle / name):
            raise Error(f"source install-bundle does not bind {destination}")


def load_bundle(
    bundle_id: str, destination: Path | None = None, *, require_authority: bool = True,
) -> dict[str, Any]:
    if not BUNDLE_ID.fullmatch(bundle_id):
        raise Error("bundle id must be 32 lowercase hexadecimal characters")
    bundle = UPLOAD_ROOT / f"security-{bundle_id}"
    try:
        info = bundle.lstat()
    except OSError as exc:
        raise Error("uploaded security-config bundle is missing") from exc
    if bundle.is_symlink() or not stat.S_ISDIR(info.st_mode) or info.st_uid != 1000 \
            or stat.S_IMODE(info.st_mode) & 0o077:
        raise Error("uploaded security-config bundle must be private and owned by thron")
    names = set(TARGETS) | {"source-manifest.json", "docker-config.json"}
    if require_authority:
        names |= {app.STAGING_RECEIPT_NAME, app.APPROVAL_NAME}
    for name in names:
        require_candidate_file(bundle / name, name)
    require_candidate_file(bundle / "security-config-manifest.json", "security-config-manifest.json")
    manifest = app.strict_load(bundle / "security-config-manifest.json")
    if set(manifest) != {"schema", "kind", "source_bundle_sha256", "files"} \
            or manifest.get("schema") != 1 \
            or manifest.get("kind") != "menhir-security-config-bundle":
        raise Error("security-config bundle manifest schema mismatch")
    files = manifest.get("files")
    if not isinstance(files, dict) or set(files) != names:
        raise Error("security-config bundle file set mismatch")
    for name in names:
        if files.get(name) != app.sha256(bundle / name):
            raise Error(f"security-config bundle digest mismatch: {name}")
    if manifest["source_bundle_sha256"] != app.sha256(bundle / "source-manifest.json"):
        raise Error("security-config source manifest digest mismatch")
    if destination is not None:
        destination.mkdir(parents=True, mode=0o700)
        os.chown(destination, 0, 0)
        for name in names | {"security-config-manifest.json"}:
            app.atomic_bytes(destination / name, (bundle / name).read_bytes(), 0o400)
        bundle = destination
    release = app.strict_load(bundle / "release.json")
    release_sha = app.sha256(bundle / "release.json")
    validate_source_manifest(
        app.strict_load(bundle / "source-manifest.json"), bundle, release_sha,
        str(release.get("release_id", "")),
    )
    validate_policy_files(bundle, release)
    return {
        "path": bundle, "release": release, "release_sha": release_sha,
        "env": app.parse_env(bundle / "production.env"),
    }
