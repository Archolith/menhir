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


SPEC_KEYS = frozenset({
    "schema", "release_id", "release_author", "repositories", "images",
    "evidence", "rendered", "network", "initial_release", "prior_release",
    "prior_route", "initial_prior_images", "secret_version_ids",
    "artifact_sources", "initial_host_state",
})
REPOSITORIES = frozenset({"menhir", "archolith_oauth", "yawn_deploy", "yawn_vps"})
EVIDENCE_DIGESTS = {
    "oauth_wheel": "oauth_wheel_sha256",
    "wheel_manifest": "wheel_manifest_sha256",
    "dockerfile_wheel_manifest": "dockerfile_wheel_manifest_sha256",
    "sbom": "sbom_sha256",
    "scan": "scan_evidence_sha256",
    "provenance": "provenance_sha256",
}
RENDERED_DESTINATIONS = {
    "/srv/menhir/production/release/production.env": "production_env_sha256",
    "/etc/yawn-vps/menhir-oauth-policy.json": "operations_policy_sha256",
    "/etc/yawn-vps/menhir-oauth-public.pem": "oauth_public_key_sha256",
    "/etc/yawn-vps/menhir-python-runtime.sha256": "python_runtime_digest_sha256",
}
RELEASE_DESTINATION = "/srv/menhir/production/release/release.json"
MANIFEST_NAME = "bundle-manifest.json"
INSTALLER_NAME = "install.sh"
INSTALLER_SOURCE_NAME = "release-install.sh"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
OID_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
ALLOWED_GIT_MODES = frozenset({"100644", "100755"})


class DuplicateKeyError(ValueError):
    """Raised when a JSON object repeats a key."""


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


def _run_git(repo: Path, arguments: Sequence[str], label: str) -> bytes:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *arguments],
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError(f"git lookup failed for {label}") from exc
    return result.stdout


def _validate_repository(
    path_value: Any, name: str, expected_remote: str, commit: str
) -> Path:
    repo = _directory(path_value, f"repository {name}")
    top = _run_git(repo, ["rev-parse", "--show-toplevel"], name).decode(
        "utf-8", errors="strict"
    ).strip()
    if Path(top).resolve(strict=True) != repo.resolve(strict=True):
        raise ValueError(f"repository {name} path is not its canonical worktree root")
    remote = _run_git(repo, ["remote", "get-url", "origin"], name).decode(
        "utf-8", errors="strict"
    ).strip()
    match = re.fullmatch(
        r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
        r"([^/]+)/([^/]+?)(?:\.git)?/?",
        remote,
    )
    canonical_remote = (
        f"https://github.com/{match.group(1)}/{match.group(2)}.git"
        if match else ""
    )
    if canonical_remote != expected_remote:
        raise ValueError(f"repository {name} origin is inconsistent with release authority")
    object_type = _run_git(repo, ["cat-file", "-t", f"{commit}^{{commit}}"], name)
    if object_type != b"commit\n":
        raise ValueError(f"repository {name} release-bound commit is missing")
    return repo


def _git_blob(
    repo: Path, commit: str, source_path: str, expected_oid: str, label: str
) -> bytes:
    source_path = _canonical_source_path(source_path, label)
    raw = _run_git(
        repo,
        ["ls-tree", "-z", commit, "--", f":(literal){source_path}"],
        label,
    )
    records = [record for record in raw.split(b"\0") if record]
    if len(records) != 1:
        raise ValueError(f"{label} is missing or is not one exact committed object")
    try:
        metadata, encoded_path = records[0].split(b"\t", 1)
        mode, object_type, oid = metadata.decode("ascii").split(" ")
        observed_path = encoded_path.decode("utf-8", errors="strict")
    except (ValueError, UnicodeError) as exc:
        raise ValueError(f"{label} has an invalid git tree record") from exc
    if observed_path != source_path:
        raise ValueError(f"{label} git tree path is inconsistent")
    if mode not in ALLOWED_GIT_MODES or object_type != "blob":
        raise ValueError(
            f"{label} has unsafe or unknown git mode/type: {mode} {object_type}"
        )
    if not OID_RE.fullmatch(oid) or oid != expected_oid:
        raise ValueError(f"{label} blob object id differs from release authority")
    return _run_git(repo, ["cat-file", "blob", oid], label)


def _validate_spec_relationship(
    release: dict[str, Any], spec: dict[str, Any]
) -> tuple[dict[str, Path], dict[str, Path]]:
    _exact_keys(spec, SPEC_KEYS, "release spec")
    if spec.get("schema") != 1:
        raise ValueError("release spec schema must be 1")
    for key in ("release_id", "release_author", "images", "network", "secret_version_ids"):
        if spec.get(key) != release.get(key):
            raise ValueError(f"release spec {key} differs from release authority")
    repositories = _exact_keys(
        spec.get("repositories"), REPOSITORIES, "release spec repositories"
    )
    repos = _exact_keys(release.get("repos"), REPOSITORIES, "release repositories")
    remotes = _exact_keys(
        release.get("repo_remotes"), REPOSITORIES, "release repository remotes"
    )
    repo_paths = {
        name: _validate_repository(
            repositories[name], name, remotes[name], repos[name]
        )
        for name in sorted(REPOSITORIES)
    }

    rendered_values = spec.get("rendered")
    if not isinstance(rendered_values, dict) or set(rendered_values) != set(release["rendered"]):
        raise ValueError("release spec rendered keys differ from release authority")
    rendered_paths: dict[str, Path] = {}
    for key, expected_digest in sorted(release["rendered"].items()):
        value = rendered_values[key]
        if isinstance(value, str) and value.startswith("sha256:"):
            if value.removeprefix("sha256:") != expected_digest:
                raise ValueError(f"release spec rendered.{key} differs from release authority")
            continue
        path = _regular_file(value, f"release spec rendered.{key}")
        if _sha256_file(path) != expected_digest:
            raise ValueError(f"release spec rendered.{key} digest drift")
        rendered_paths[key] = path

    evidence_keys = frozenset(EVIDENCE_DIGESTS) | frozenset({"wheelhouse"})
    evidence = _exact_keys(
        spec.get("evidence"), evidence_keys, "release spec evidence"
    )
    _directory(evidence["wheelhouse"], "release spec evidence.wheelhouse")
    for key, release_key in EVIDENCE_DIGESTS.items():
        path = _regular_file(evidence.get(key), f"release spec evidence.{key}")
        if _sha256_file(path) != release[release_key]:
            raise ValueError(f"release spec evidence.{key} digest drift")

    initial = spec.get("initial_release")
    if initial is not release["rollback_anchors"]["initial_release"]:
        raise ValueError("release spec initial_release differs from release authority")
    prior_route = _regular_file(spec.get("prior_route"), "release spec prior_route")
    if _sha256_file(prior_route) != release["rollback_anchors"]["prior_route_sha256"]:
        raise ValueError("release spec prior_route digest drift")
    if initial:
        if spec.get("initial_prior_images") \
                != release["rollback_anchors"]["prior_images"]:
            raise ValueError("release spec prior images differ from release authority")
        initial_host = _regular_file(
            spec.get("initial_host_state"), "release spec initial_host_state"
        )
        if _sha256_file(initial_host) \
                != release["rollback_anchors"]["initial_host_state_sha256"]:
            raise ValueError("release spec initial_host_state digest drift")
        if spec.get("prior_release") is not None:
            raise ValueError("initial release spec must not provide prior_release")
    else:
        if spec.get("initial_host_state") is not None:
            raise ValueError("non-initial release spec must not provide initial_host_state")
        prior = _regular_file(spec.get("prior_release"), "release spec prior_release")
        if _sha256_file(prior) != release["rollback_anchors"]["prior_release_sha256"]:
            raise ValueError("release spec prior_release digest drift")
        prior_release = menhir_schema.validate_release(str(prior))
        rollback = release["rollback_anchors"]
        if rollback["prior_release_id"] != prior_release["release_id"] \
                or rollback["prior_images"] != {
                    name: prior_release["images"][name]
                    for name in ("menhir", "neo4j", "caddy")
                }:
            raise ValueError("release spec prior_release authority mismatch")
    return repo_paths, rendered_paths


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
