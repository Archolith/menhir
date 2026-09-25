#!/usr/bin/env python3
"""Build a release-bound, exact-census Menhir installation bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Sequence


SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

import menhir_schema  # noqa: E402
import release_spec  # noqa: E402

if str(SCRIPT_DIR) not in sys.path:
    # The sibling parts package lives beside this script; make it importable even
    # when this module is loaded by path (importlib spec) rather than as a script.
    sys.path.append(str(SCRIPT_DIR))

from build_install_bundle_parts import (  # noqa: E402
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
    _bundle_census as _bundle_census,
    _canonical_destination as _canonical_destination,
    _canonical_source_path as _canonical_source_path,
    _destination_mode as _destination_mode,
    _directory as _directory,
    _exact_keys as _exact_keys,
    _git_blob as _git_blob,
    _load_installed_destinations as _load_installed_destinations,
    _manifest_bytes as _manifest_bytes,
    _normalize_timestamps as _normalize_timestamps,
    _output_fence as _output_fence,
    _regular_file as _regular_file,
    _remove_tree as _remove_tree,
    _run_git as _run_git,
    _sha256_bytes as _sha256_bytes,
    _sha256_file as _sha256_file,
    _strict_json as _strict_json,
    _unique_object as _unique_object,
    _validate_bundle as _validate_bundle,
    _validate_repository as _validate_repository,
    _validate_spec_relationship as _validate_spec_relationship,
    _write_file as _write_file,
)


def _build_install_bundle(
    release_path: Path,
    spec_path: Path,
    output_path: Path,
    installer_path: Path,
    workspace_root: Path,
) -> dict[str, Any]:
    release_path = _regular_file(release_path, "release")
    spec_path = _regular_file(spec_path, "release spec")
    installer_path = _regular_file(installer_path, "installer")
    output_path, _ = _output_fence(output_path, workspace_root)
    _strict_json(release_path, "release")
    release = menhir_schema.validate_release(str(release_path))
    spec = _strict_json(spec_path, "release spec")
    repo_paths, rendered_paths = _validate_spec_relationship(release, spec)

    installed = _load_installed_destinations()
    allowed = installed | frozenset({RELEASE_DESTINATION})
    artifacts = release.get("artifacts")
    if not isinstance(artifacts, dict) or set(artifacts) != installed:
        missing = sorted(installed - set(artifacts or {}))
        extra = sorted(set(artifacts or {}) - installed)
        raise ValueError(
            f"release artifacts differ from installed-artifacts.json: "
            f"missing={missing}, extra={extra}"
        )
    sources = spec.get("artifact_sources")
    if not isinstance(sources, dict) or set(sources) != set(artifacts):
        raise ValueError("release spec artifact_sources differ from release artifacts")

    payloads: dict[str, bytes] = {RELEASE_DESTINATION: release_path.read_bytes()}
    for destination, entry in sorted(artifacts.items()):
        _canonical_destination(destination, allowed, "release artifact destination")
        if not isinstance(entry, dict):
            raise ValueError(f"release artifact entry is invalid: {destination}")
        source = sources[destination]
        kind = entry.get("kind")
        if kind == "git":
            expected_source = {
                "kind": "git",
                "repository": entry.get("repository"),
                "path": entry.get("path"),
            }
            if source != expected_source:
                raise ValueError(f"release/spec git artifact mismatch: {destination}")
            repository = entry.get("repository")
            commit = entry.get("commit")
            if repository not in REPOSITORIES \
                    or commit != release["repos"].get(repository):
                raise ValueError(f"inconsistent repository authority: {destination}")
            payload = _git_blob(
                repo_paths[repository],
                commit,
                entry.get("path"),
                entry.get("blob_oid"),
                f"release artifact {destination}",
            )
        elif kind == "rendered":
            rendered_key = entry.get("rendered_key")
            if source != {"kind": "rendered", "rendered_key": rendered_key} \
                    or RENDERED_DESTINATIONS.get(destination) != rendered_key \
                    or rendered_key not in rendered_paths:
                raise ValueError(f"release/spec rendered artifact mismatch: {destination}")
            payload = rendered_paths[rendered_key].read_bytes()
        else:
            raise ValueError(f"unexpected release artifact kind: {kind!r}")
        if _sha256_bytes(payload) != entry.get("sha256"):
            raise ValueError(f"release artifact digest mismatch: {destination}")
        payloads[destination] = payload

    installer_payload = installer_path.read_bytes()
    installer_digest = _sha256_bytes(installer_payload)
    manifest = {
        "schema": 1,
        "kind": "menhir-release-install-bundle",
        "release_id": release["release_id"],
        "release_sha256": _sha256_bytes(payloads[RELEASE_DESTINATION]),
        "files": {
            destination: {
                "mode": _destination_mode(destination),
                "sha256": _sha256_bytes(payload),
            }
            for destination, payload in sorted(payloads.items())
        },
    }

    temporary = Path(tempfile.mkdtemp(
        prefix=f".{output_path.name}.tmp-", dir=str(output_path.parent)
    ))
    published = False
    try:
        _write_file(temporary / INSTALLER_NAME, installer_payload, "0755")
        for destination, payload in sorted(payloads.items()):
            _write_file(
                temporary / ("rootfs" + destination).lstrip("/"),
                payload,
                _destination_mode(destination),
            )
        _write_file(temporary / MANIFEST_NAME, _manifest_bytes(manifest), "0644")
        _validate_bundle(temporary, installer_digest)
        _normalize_timestamps(temporary)
        _validate_bundle(temporary, installer_digest)
        if output_path.exists() or output_path.is_symlink():
            raise ValueError("output appeared while bundle was being built")
        os.rename(temporary, output_path)
        published = True
    finally:
        if not published and temporary.exists():
            _remove_tree(temporary)
    return manifest


def build_install_bundle(
    release_path: Path,
    spec_path: Path,
    output_path: Path,
    installer_path: Path | None = None,
) -> dict[str, Any]:
    """Build under the workspace root defined by the release spec's directory."""
    release = Path(release_path)
    spec = Path(spec_path)
    output = Path(output_path)
    installer = Path(installer_path) if installer_path is not None \
        else SCRIPT_DIR / INSTALLER_SOURCE_NAME
    return _build_install_bundle(
        release, spec, output, installer, spec.parent
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("release", type=Path)
    parser.add_argument("spec", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument(
        "--installer", type=Path, default=SCRIPT_DIR / INSTALLER_SOURCE_NAME
    )
    parser.add_argument(
        "--workspace-root",
        type=Path,
        help="explicit output fence (defaults to the release spec directory)",
    )
    args = parser.parse_args(argv)
    root = args.workspace_root if args.workspace_root is not None else args.spec.parent
    try:
        _build_install_bundle(
            args.release, args.spec, args.output, args.installer, root
        )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
