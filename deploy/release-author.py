#!/usr/bin/env python3
"""Author one canonical four-repository Menhir production release record."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

import menhir_schema  # noqa: E402
import verify_wheelhouse  # noqa: E402


SPEC_KEYS = frozenset({
    "schema", "release_id", "repositories", "images", "evidence",
    "rendered", "network", "initial_release", "prior_release",
    "prior_route", "initial_prior_images", "secret_version_ids",
    "artifact_sources", "source_fence_key_id", "source_fence_public_key",
    "source_fence_tls_ca", "external_evidence_public_keys",
    "initial_host_state",
})
REPOSITORIES = frozenset({"menhir", "archolith_oauth", "yawn_deploy", "yawn_vps"})
IMAGES = frozenset({"menhir", "neo4j", "caddy", "base"})
EVIDENCE = frozenset({
    "oauth_wheel", "wheelhouse", "wheel_manifest",
    "dockerfile_wheel_manifest", "sbom", "scan", "provenance",
})
RENDERED = frozenset({
    "menhir_compose_sha256", "yawn_compose_sha256", "caddy_sha256",
    "registry_sha256", "policy_sha256", "yawn_env_sha256",
    "production_env_sha256", "operations_policy_sha256",
    "oauth_public_key_sha256",
})
SECRET_VERSIONS = frozenset({
    "neo4j-auth", "neo4j-password", "oauth-signing-key",
    "oauth-retry-keyring", "oauth-consent-secret", "operator-key",
    "client-policy", "provider-key",
    "source-fence-token",
})
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
ALLOWED_ARTIFACT_PREFIXES = ("/srv/menhir/production/", "/srv/yawn/projects/",
                             "/etc/sudoers.d/", "/etc/systemd/system/",
                             "/etc/tmpfiles.d/", "/etc/yawn-vps/",
                             "/usr/local/sbin/")

# Safe release_id contract (blocker 8): only this shape is accepted, so a
# caller-supplied label cannot smuggle path/traversal/metacharacter content.
RELEASE_ID_RE = re.compile(r"^menhir-prod-[0-9]+\.[0-9]+\.[0-9]+-[0-9]+$")

# Expected git remote origin identities for the four release repositories.
# A repo whose origin does not match its expected identity is refused so a
# rebuild from a fork/mirror or a substituted remote is never recorded as a
# release input.
EXPECTED_REPO_REMOTES = menhir_schema.EXPECTED_REPO_REMOTES
RENDERED_ARTIFACT_DESTINATIONS = {
    "/srv/menhir/production/release/production.env": "production_env_sha256",
    "/etc/yawn-vps/menhir-oauth-policy.json": "operations_policy_sha256",
    "/etc/yawn-vps/menhir-oauth-public.pem": "oauth_public_key_sha256",
}


def _installed_destinations() -> frozenset[str]:
    value = json.loads((SCRIPT_DIR / "installed-artifacts.json").read_text(encoding="utf-8"))
    if set(value) != {"schema", "destinations"} or value["schema"] != 1:
        raise RuntimeError("installed-artifacts.json schema is invalid")
    rows = value["destinations"]
    if not isinstance(rows, list) or not rows or len(rows) != len(set(rows)) \
            or any(not isinstance(row, str) or not row.startswith("/") for row in rows):
        raise RuntimeError("installed-artifacts.json destinations are invalid")
    return frozenset(rows)


REQUIRED_ARTIFACT_DESTINATIONS = _installed_destinations()


def _exact(value: Any, keys: frozenset[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        missing = sorted(keys - set(value)) if isinstance(value, dict) else sorted(keys)
        extra = sorted(set(value) - keys) if isinstance(value, dict) else []
        raise ValueError(f"{label} keys mismatch: missing={missing}, extra={extra}")
    return value


def _regular(path_value: Any, label: str) -> Path:
    if not isinstance(path_value, str) or not path_value:
        raise ValueError(f"{label} must be a path string")
    path = Path(path_value)
    if not path.is_absolute():
        raise ValueError(f"{label} must be absolute")
    path.lstat()
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"{label} must be a regular non-symlink file")
    return path


def _directory(path_value: Any, label: str) -> Path:
    if not isinstance(path_value, str) or not path_value:
        raise ValueError(f"{label} must be a path string")
    path = Path(path_value)
    if not path.is_absolute() or not path.is_dir() or path.is_symlink():
        raise ValueError(f"{label} must be an absolute non-symlink directory")
    return path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _canonical_remote(value: str) -> str:
    match = re.fullmatch(
        r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
        r"([^/]+)/([^/]+?)(?:\.git)?/?",
        value.strip(),
    )
    if not match:
        raise ValueError("origin must be a GitHub HTTPS/SSH repository URL")
    return f"https://github.com/{match.group(1)}/{match.group(2)}.git"


def _repo_identity(path_value: Any, label: str) -> tuple[Path, str, str]:
    if not isinstance(path_value, str) or not path_value:
        raise ValueError(f"repository {label} path is required")
    path = Path(path_value)
    if not path.is_absolute() or not path.is_dir() or path.is_symlink():
        raise ValueError(f"repository {label} must be an absolute non-symlink directory")
    status = subprocess.run(
        ["git", "-C", str(path), "status", "--porcelain=v1", "--untracked-files=all"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    if status:
        raise ValueError(f"repository {label} is not clean")
    head = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", head):
        raise ValueError(f"repository {label} HEAD is not an immutable commit")
    remote = subprocess.run(
        ["git", "-C", str(path), "remote", "get-url", "origin"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    canonical = _canonical_remote(remote)
    if canonical != EXPECTED_REPO_REMOTES[label]:
        raise ValueError(
            f"repository {label} origin identity mismatch: "
            f"expected {EXPECTED_REPO_REMOTES[label]}, got {canonical}"
        )
    return path, head, canonical


def _git_blob(repo: Path, commit: str, source_path: str, label: str) -> tuple[bytes, str]:
    if not isinstance(source_path, str) or not source_path or source_path.startswith("/") \
            or ".." in source_path.split("/") or "\\" in source_path:
        raise ValueError(f"{label} must be a canonical repository-relative path")
    try:
        data = subprocess.run(
            ["git", "-C", str(repo), "show", f"{commit}:{source_path}"],
            check=True,
            capture_output=True,
        ).stdout
        blob_oid = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", f"{commit}:{source_path}"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except subprocess.CalledProcessError as exc:
        raise ValueError(f"{label} is not a committed blob at {commit}") from exc
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", blob_oid):
        raise ValueError(f"{label} git blob object id is invalid")
    return data, blob_oid


def _git_package_files(repo: Path, commit: str) -> dict[str, bytes]:
    prefix = "src/archolith_oauth/"
    raw = subprocess.run(
        ["git", "-C", str(repo), "ls-tree", "-r", "--name-only", "-z",
         commit, "--", prefix],
        check=True,
        capture_output=True,
    ).stdout
    paths = [item.decode("utf-8") for item in raw.split(b"\0") if item]
    if not paths or any(not path.startswith(prefix) for path in paths):
        raise ValueError("reviewed OAuth source commit has no canonical package tree")
    return {
        path.removeprefix(prefix): subprocess.run(
            ["git", "-C", str(repo), "show", f"{commit}:{path}"],
            check=True,
            capture_output=True,
        ).stdout
        for path in paths
    }


def _source_tree_digest(files: dict[str, bytes]) -> str:
    digest = hashlib.sha256()
    for path, payload in sorted(files.items()):
        encoded = path.encode("utf-8")
        digest.update(b"oauth-wheel-source-v1\0")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(hashlib.sha256(payload).digest())
    return digest.hexdigest()


def _git_package_tree_digest(repo: Path, commit: str) -> str:
    return _source_tree_digest(_git_package_files(repo, commit))


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
        metadata_members = [
            item.filename for item in infos
            if item.filename.count("/") == 1
            and item.filename.split("/", 1)[0].startswith("archolith_oauth-")
            and item.filename.split("/", 1)[0].endswith(".dist-info")
        ]
        allowed_metadata_names = {"METADATA", "WHEEL", "RECORD"}
        if (
            sum(name.endswith("/METADATA") for name in metadata_members) != 1
            or any(name.rsplit("/", 1)[1] not in allowed_metadata_names
                   for name in metadata_members)
        ):
            raise ValueError("OAuth wheel metadata layout is not authorized")
        reviewed_members = {
            item.filename for item in package_infos.values()
        } | set(metadata_members)
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


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid {label} JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _validate_provenance(
    path: Path,
    repos: dict[str, str],
    repo_remotes: dict[str, str],
    images: dict[str, str],
    oauth_sha: str,
    wheel_manifest_sha: str,
    docker_manifest_sha: str,
) -> None:
    value = _load_json(path, "provenance")
    expected_keys = frozenset({
        "schema", "repos", "repo_remotes", "images", "oauth_wheel_sha256",
        "wheel_manifest_sha256", "dockerfile_wheel_manifest_sha256",
    })
    _exact(value, expected_keys, "provenance")
    expected = {
        "schema": 1,
        "repos": repos,
        "repo_remotes": repo_remotes,
        "images": images,
        "oauth_wheel_sha256": oauth_sha,
        "wheel_manifest_sha256": wheel_manifest_sha,
        "dockerfile_wheel_manifest_sha256": docker_manifest_sha,
    }
    if value != expected:
        raise ValueError("provenance does not bind the exact release inputs")


def author_release(spec_path: Path, output_path: Path) -> dict[str, Any]:
    spec_path = _regular(str(spec_path), "spec")
    spec = _exact(_load_json(spec_path, "release spec"), SPEC_KEYS, "release spec")
    if spec.get("schema") != 1:
        raise ValueError("release spec schema must be 1")
    release_id = spec.get("release_id")
    if not isinstance(release_id, str) or not RELEASE_ID_RE.fullmatch(release_id) \
            or len(release_id) > 64:
        raise ValueError(
            "release_id must match menhir-prod-<major>.<minor>.<patch>-<sequence>"
        )

    repo_paths = _exact(spec.get("repositories"), REPOSITORIES, "repositories")
    repo_identities = {
        name: _repo_identity(repo_paths[name], name) for name in sorted(REPOSITORIES)
    }
    repos = {name: repo_identities[name][1] for name in sorted(REPOSITORIES)}
    repo_remotes = {name: repo_identities[name][2] for name in sorted(REPOSITORIES)}

    images = _exact(spec.get("images"), IMAGES, "images")
    for name, digest in images.items():
        if not isinstance(digest, str) or not DIGEST_RE.fullmatch(digest):
            raise ValueError(f"images.{name} must be a sha256 digest")

    evidence_values = _exact(spec.get("evidence"), EVIDENCE, "evidence")
    evidence = {
        name: (
            _directory(evidence_values[name], f"evidence.{name}")
            if name == "wheelhouse"
            else _regular(evidence_values[name], f"evidence.{name}")
        )
        for name in EVIDENCE
    }
    oauth_sha = _sha256(evidence["oauth_wheel"])
    oauth_repo, oauth_commit, _ = repo_identities["archolith_oauth"]
    oauth_source_tree_sha = _bind_oauth_wheel_source(
        oauth_repo, oauth_commit, evidence["oauth_wheel"]
    )
    wheel_manifest_sha = _sha256(evidence["wheel_manifest"])
    docker_manifest_sha = _sha256(evidence["dockerfile_wheel_manifest"])
    verify_wheelhouse.verify(
        evidence["wheelhouse"],
        evidence["dockerfile_wheel_manifest"],
        docker_manifest_sha,
        oauth_sha,
    )
    _validate_provenance(
        evidence["provenance"], repos, repo_remotes, images, oauth_sha,
        wheel_manifest_sha, docker_manifest_sha,
    )

    rendered_values = _exact(spec.get("rendered"), RENDERED, "rendered")
    rendered = {
        key: _sha256(_regular(rendered_values[key], f"rendered.{key}"))
        for key in sorted(RENDERED)
    }
    network = spec.get("network")
    if not isinstance(network, dict):
        raise ValueError("network must be an object")

    initial = spec.get("initial_release")
    if not isinstance(initial, bool):
        raise ValueError("initial_release must be boolean")
    prior_value = spec.get("prior_release")
    prior_route_path = _regular(spec.get("prior_route"), "prior_route")
    prior_route_sha = _sha256(prior_route_path)
    initial_host_state = spec.get("initial_host_state")
    if initial:
        if prior_value is not None:
            raise ValueError("initial release must not supply prior_release")
        initial_images = _exact(
            spec.get("initial_prior_images"),
            frozenset({"menhir", "neo4j", "caddy"}),
            "initial_prior_images",
        )
        for name, digest in initial_images.items():
            if not isinstance(digest, str) or not DIGEST_RE.fullmatch(digest):
                raise ValueError(f"initial_prior_images.{name} must be a sha256 digest")
        rollback = {
            "initial_release": True,
            "prior_release_id": "",
            "prior_release_sha256": "",
            "prior_images": initial_images,
            "prior_route_sha256": prior_route_sha,
            "initial_host_state_sha256": _sha256(
                _regular(initial_host_state, "initial_host_state")
            ),
        }
    else:
        if not isinstance(prior_value, str) or not prior_value:
            raise ValueError("non-initial release requires prior_release")
        prior_path = _regular(prior_value, "prior_release")
        prior = menhir_schema.validate_release(str(prior_path))
        if initial_host_state is not None:
            raise ValueError("non-initial release must not supply initial_host_state")
        rollback = {
            "initial_release": False,
            "prior_release_id": prior["release_id"],
            "prior_release_sha256": _sha256(prior_path),
            "prior_images": {
                name: prior["images"][name] for name in ("menhir", "neo4j", "caddy")
            },
            "prior_route_sha256": prior_route_sha,
            "initial_host_state_sha256": "",
        }

    secret_versions = _exact(
        spec.get("secret_version_ids"), SECRET_VERSIONS, "secret_version_ids"
    )
    artifact_sources = spec.get("artifact_sources")
    if not isinstance(artifact_sources, dict) or set(artifact_sources) != REQUIRED_ARTIFACT_DESTINATIONS:
        missing = sorted(REQUIRED_ARTIFACT_DESTINATIONS - set(artifact_sources or {}))
        extra = sorted(set(artifact_sources or {}) - REQUIRED_ARTIFACT_DESTINATIONS)
        raise ValueError(
            "artifact_sources must match installed-artifacts.json: "
            f"missing={missing}, extra={extra}"
        )
    artifacts: dict[str, dict[str, str]] = {}
    for destination, source in sorted(artifact_sources.items()):
        if destination in RENDERED_ARTIFACT_DESTINATIONS:
            rendered_key = RENDERED_ARTIFACT_DESTINATIONS[destination]
            if source != {"kind": "rendered", "rendered_key": rendered_key}:
                raise ValueError(
                    f"artifact_sources[{destination}] must reference rendered {rendered_key}"
                )
            artifacts[destination] = {
                "kind": "rendered",
                "sha256": rendered[rendered_key],
                "rendered_key": rendered_key,
            }
            continue
        if not isinstance(source, dict) or set(source) != {"kind", "repository", "path"} \
                or source.get("kind") != "git" or source.get("repository") not in REPOSITORIES:
            raise ValueError(
                f"artifact_sources[{destination}] must be a committed git blob mapping"
            )
        repository = source["repository"]
        source_path = source.get("path")
        repo_path, commit, _ = repo_identities[repository]
        data, blob_oid = _git_blob(
            repo_path, commit, source_path, f"artifact_sources[{destination}]"
        )
        artifacts[destination] = {
            "kind": "git",
            "sha256": hashlib.sha256(data).hexdigest(),
            "repository": repository,
            "commit": commit,
            "path": source_path,
            "blob_oid": blob_oid,
        }

    source_fence_key_id = spec.get("source_fence_key_id")
    if not isinstance(source_fence_key_id, str) or len(source_fence_key_id) > 64 \
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", source_fence_key_id):
        raise ValueError("source_fence_key_id must be a safe bounded key id")
    source_fence_public_key = spec.get("source_fence_public_key")
    menhir_schema._decode_ed25519_public_key(  # release-schema authority
        source_fence_public_key, "source_fence_public_key"
    )
    source_fence_tls_ca_sha256 = _sha256(
        _regular(spec.get("source_fence_tls_ca"), "source_fence_tls_ca")
    )
    external_evidence_public_keys = spec.get("external_evidence_public_keys")
    if not isinstance(external_evidence_public_keys, dict) or len(external_evidence_public_keys) < 2:
        raise ValueError("external_evidence_public_keys must contain at least two workers")
    for worker_id, public_key in external_evidence_public_keys.items():
        if not isinstance(worker_id, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", worker_id
        ):
            raise ValueError("external evidence worker id is invalid")
        menhir_schema._decode_ed25519_public_key(
            public_key, f"external_evidence_public_keys.{worker_id}"
        )

    release = {
        "schema": 1,
        "release_id": release_id,
        "repos": repos,
        "repo_remotes": repo_remotes,
        "oauth_wheel_sha256": oauth_sha,
        "oauth_wheel_source": {
            "repository": "archolith_oauth",
            "commit": oauth_commit,
            "source_tree_sha256": oauth_source_tree_sha,
            "wheel_sha256": oauth_sha,
        },
        "images": images,
        "wheel_manifest_sha256": wheel_manifest_sha,
        "dockerfile_wheel_manifest_sha256": docker_manifest_sha,
        "sbom_sha256": _sha256(evidence["sbom"]),
        "scan_evidence_sha256": _sha256(evidence["scan"]),
        "provenance_sha256": _sha256(evidence["provenance"]),
        "rendered": rendered,
        "network": network,
        "rollback_anchors": rollback,
        "secret_version_ids": secret_versions,
        "artifacts": artifacts,
        "source_fence_key_id": source_fence_key_id,
        "source_fence_public_key": source_fence_public_key,
        "source_fence_tls_ca_sha256": source_fence_tls_ca_sha256,
        "external_evidence_public_keys": external_evidence_public_keys,
    }

    parent = output_path.resolve().parent
    parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".release.", dir=parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(release, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o400)
        menhir_schema.validate_release(temporary)
        os.replace(temporary, output_path)
        _fsync_directory(parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return release


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv[1:])
    try:
        author_release(args.spec, args.output)
    except (OSError, subprocess.CalledProcessError, ValueError) as exc:
        print(f"release authoring failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
