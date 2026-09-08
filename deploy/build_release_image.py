#!/usr/bin/env python3
"""Build, seal, verify, and publish one release-labelled Menhir image."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Sequence

SHA256_RE = re.compile(r"[0-9a-f]{64}")
DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
VERSION_RE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+-[0-9]+")
PUSH_DIGEST_RE = re.compile(r"digest:\s+(sha256:[0-9a-f]{64})")
REMOTE_DIGEST_RE = re.compile(r"(?m)^Digest:\s+(sha256:[0-9a-f]{64})\s*$")
LABELS = {
    "commit": "org.opencontainers.image.revision",
    "version": "org.opencontainers.image.version",
    "wheel_manifest_sha256": "org.archolith.menhir.wheel-manifest.sha256",
    "oauth_wheel_sha256": "org.archolith.oauth.wheel.sha256",
}
EVIDENCE_FILES = {
    "sbom": "release-image-evidence/sbom",
    "vulnerability_scan": "release-image-evidence/scan",
}


class BuildImageError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def oauth_wheel_sha256(wheelhouse: Path) -> str:
    manifest = wheelhouse / "SHA256SUMS"
    if not manifest.is_file() or manifest.is_symlink():
        raise BuildImageError(f"wheel manifest is missing or unsafe: {manifest}")
    matches: list[str] = []
    for number, raw in enumerate(manifest.read_text(encoding="ascii").splitlines(), 1):
        parts = raw.split(maxsplit=1)
        if len(parts) != 2 or SHA256_RE.fullmatch(parts[0]) is None:
            raise BuildImageError(f"malformed SHA256SUMS line {number}")
        relative = parts[1].lstrip("* ")
        if "/" in relative or "\\" in relative or relative in {"", ".", ".."}:
            raise BuildImageError(f"unsafe wheel path on SHA256SUMS line {number}")
        wheel = wheelhouse / relative
        if not wheel.is_file() or wheel.is_symlink():
            raise BuildImageError(f"manifest wheel is missing or unsafe: {relative}")
        actual = sha256_file(wheel)
        if actual != parts[0]:
            raise BuildImageError(f"wheel digest mismatch: {relative}")
        normalized = relative.lower().replace("-", "_")
        if normalized.startswith("archolith_oauth_") and normalized.endswith(".whl"):
            matches.append(actual)
    if len(matches) != 1:
        raise BuildImageError("wheelhouse must contain exactly one archolith_oauth wheel")
    return matches[0]


def git_commit(repo: Path) -> str:
    for args, label in [(["diff", "--quiet"], "tracked worktree"),
                        (["diff", "--cached", "--quiet"], "index")]:
        result = subprocess.run(["git", "-C", str(repo), *args], check=False)
        if result.returncode != 0:
            raise BuildImageError(f"Git {label} is not clean")
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    )
    commit = result.stdout.strip()
    if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise BuildImageError("Git HEAD is not a full commit digest")
    return commit


def build_command(*, repo: Path, dockerfile: Path, image_tag: str, python_base: str,
                  commit: str, version: str, wheel_manifest: str,
                  oauth_wheel: str) -> list[str]:
    return [
        "docker", "build", "--file", str(dockerfile),
        "--build-arg", f"PYTHON_BASE={python_base}",
        "--build-arg", f"RELEASE_COMMIT={commit}",
        "--build-arg", f"RELEASE_VERSION={version}",
        "--build-arg", f"WHEEL_MANIFEST_SHA256={wheel_manifest}",
        "--build-arg", f"OAUTH_WHEEL_SHA256={oauth_wheel}",
        "--tag", image_tag, str(repo),
    ]


def inspect_image(image_tag: str, expected: dict[str, str]) -> tuple[str, dict[str, str], str]:
    result = subprocess.run(
        ["docker", "image", "inspect", image_tag],
        check=True, capture_output=True, text=True,
    )
    rows = json.loads(result.stdout)
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise BuildImageError("docker image inspect returned an unexpected shape")
    image_id = rows[0].get("Id")
    config = rows[0].get("Config")
    if not isinstance(image_id, str) or DIGEST_RE.fullmatch(image_id) is None:
        raise BuildImageError("built image ID is missing or malformed")
    if not isinstance(config, dict):
        raise BuildImageError("built image config is missing")
    labels = config.get("Labels") or {}
    if not isinstance(labels, dict):
        raise BuildImageError("built image labels are malformed")
    actual = {name: labels.get(label) for name, label in LABELS.items()}
    if actual != expected:
        raise BuildImageError(f"built image labels do not match derived inputs: {actual!r}")
    encoded = json.dumps(
        config, ensure_ascii=True, separators=(",", ":"), sort_keys=True,
    ).encode("ascii")
    return image_id, actual, hashlib.sha256(encoded).hexdigest()


def _safe_relative_file(root: Path, value: str, label: str) -> Path:
    relative = Path(value)
    if (not value or relative.is_absolute() or "\\" in value
            or any(part in {"", ".", ".."} for part in relative.parts)):
        raise BuildImageError(f"{label} must be a normalized repository-relative path")
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise BuildImageError(f"{label} must not traverse a symlink")
    try:
        candidate = current.resolve(strict=True)
        candidate.relative_to(root)
    except (FileNotFoundError, ValueError):
        raise BuildImageError(f"{label} must remain inside the checkout") from None
    if not candidate.is_file() or candidate.is_symlink():
        raise BuildImageError(f"{label} does not name a regular file")
    return candidate


def _copy_evidence(source: Path, destination: Path) -> str:
    if destination.parent.is_symlink():
        raise BuildImageError("evidence destination must not traverse a symlink")
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        destination.parent.resolve(strict=True).relative_to(destination.parent.parent.resolve())
    except ValueError:
        raise BuildImageError("evidence destination escaped the artifact directory") from None
    if destination.exists() or destination.is_symlink():
        raise BuildImageError(f"evidence destination already exists: {destination}")
    with source.open("rb") as incoming, destination.open("xb") as outgoing:
        shutil.copyfileobj(incoming, outgoing)
        outgoing.flush()
        os.fsync(outgoing.fileno())
    return sha256_file(destination)


def stage_evidence(*, repo: Path, artifact_root: Path, sbom: str | None,
                   scan: str | None, required: bool) -> dict[str, Any]:
    supplied = {"sbom": sbom, "vulnerability_scan": scan}
    if required and not all(supplied.values()):
        raise BuildImageError("SBOM and vulnerability scan evidence are required for publication")
    result: dict[str, Any] = {"required": required}
    for kind, value in supplied.items():
        if not value:
            result[kind] = None
            continue
        source = _safe_relative_file(repo, value, kind)
        artifact_path = EVIDENCE_FILES[kind]
        result[kind] = {
            "artifact_path": artifact_path,
            "sha256": _copy_evidence(source, artifact_root / artifact_path),
        }
    return result


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _read_json(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise BuildImageError(f"{label} is missing or unsafe")
    value = json.loads(path.read_text(encoding="ascii"))
    if not isinstance(value, dict):
        raise BuildImageError(f"{label} must contain a JSON object")
    return value


def _require_sha256(value: Any, label: str, *, prefixed: bool = False) -> str:
    pattern = DIGEST_RE if prefixed else SHA256_RE
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise BuildImageError(f"{label} is missing or malformed")
    return value


def _verify_artifact_file(root: Path, relative: str, label: str) -> Path:
    allowed = {
        "release-image.tar",
        "release-image-metadata.json",
        "release-image-identity.json",
        *EVIDENCE_FILES.values(),
    }
    if relative not in allowed:
        raise BuildImageError(f"{label} has an unexpected artifact path")
    return _safe_relative_file(root, relative, label)


def create_validation_bundle(args: argparse.Namespace) -> dict[str, Any]:
    repo = args.repo.resolve()
    dockerfile = (repo / args.dockerfile).resolve()
    wheelhouse = (repo / args.wheelhouse).resolve()
    commit = git_commit(repo)
    if args.expected_commit and commit != args.expected_commit:
        raise BuildImageError("checked-out commit does not match the resolved validation commit")
    if not dockerfile.is_file() or not wheelhouse.is_dir():
        raise BuildImageError("Dockerfile or wheelhouse is missing")
    if not args.output or not args.identity or not args.image_archive:
        raise BuildImageError("build mode requires metadata, identity, and archive outputs")

    wheel_manifest = sha256_file(wheelhouse / "SHA256SUMS")
    oauth_wheel = oauth_wheel_sha256(wheelhouse)
    image_tag = f"{args.image}:{args.version}"
    expected = {
        "commit": commit,
        "version": args.version,
        "wheel_manifest_sha256": wheel_manifest,
        "oauth_wheel_sha256": oauth_wheel,
    }
    subprocess.run(build_command(
        repo=repo, dockerfile=dockerfile, image_tag=image_tag,
        python_base=args.python_base, commit=commit, version=args.version,
        wheel_manifest=wheel_manifest, oauth_wheel=oauth_wheel,
    ), check=True)
    image_id, labels, config_sha256 = inspect_image(image_tag, expected)

    archive_output = args.image_archive.absolute()
    if archive_output.exists() or archive_output.is_symlink():
        raise BuildImageError("image archive output already exists")
    archive = archive_output.resolve()
    subprocess.run(
        ["docker", "image", "save", "--output", str(archive), image_tag], check=True,
    )
    if not archive.is_file() or archive.is_symlink():
        raise BuildImageError("docker did not create a safe image archive")
    archive_sha256 = sha256_file(archive)
    artifact_root = args.output.resolve().parent
    evidence = stage_evidence(
        repo=repo, artifact_root=artifact_root, sbom=args.sbom,
        scan=args.scan, required=args.require_evidence,
    )
    metadata = {
        "schema": 2,
        "source_commit": commit,
        "image_tag": image_tag,
        "image_id": image_id,
        "config_sha256": config_sha256,
        "image_archive_sha256": archive_sha256,
        "python_base": args.python_base,
        "labels": labels,
        "evidence": evidence,
    }
    metadata_path = args.output.resolve()
    atomic_json(metadata_path, metadata)
    identity = {
        "schema": 1,
        "source_commit": commit,
        "image_tag": image_tag,
        "image_id": image_id,
        "config_sha256": config_sha256,
        "image_archive": {
            "artifact_path": "release-image.tar",
            "sha256": archive_sha256,
        },
        "metadata": {
            "artifact_path": "release-image-metadata.json",
            "sha256": sha256_file(metadata_path),
        },
        "evidence": {
            kind: entry["sha256"] if entry else None
            for kind, entry in evidence.items() if kind != "required"
        },
    }
    atomic_json(args.identity.resolve(), identity)
    return metadata


def verify_validation_bundle(args: argparse.Namespace, *, load_image: bool) -> tuple[dict[str, Any], dict[str, Any]]:
    if not args.metadata or not args.identity or not args.image_archive:
        raise BuildImageError("verification requires metadata, identity, and image archive inputs")
    metadata_input = args.metadata.absolute()
    if metadata_input.parent.is_symlink():
        raise BuildImageError("validation artifact directory must not be a symlink")
    artifact_root = metadata_input.parent.resolve(strict=True)
    metadata_path = _verify_artifact_file(
        artifact_root, "release-image-metadata.json", "validation metadata",
    )
    identity_path = _verify_artifact_file(
        artifact_root, "release-image-identity.json", "identity manifest",
    )
    archive = _verify_artifact_file(artifact_root, "release-image.tar", "image archive")
    if metadata_input.resolve() != metadata_path:
        raise BuildImageError("metadata input is not the fixed validation artifact")
    if args.identity.resolve() != identity_path or args.image_archive.resolve() != archive:
        raise BuildImageError("identity or archive input is not the fixed validation artifact")
    expected_identity = _require_sha256(args.expected_identity_sha256, "expected identity SHA-256")
    if sha256_file(identity_path) != expected_identity:
        raise BuildImageError("downloaded identity manifest digest does not match validation output")
    identity = _read_json(identity_path, "identity manifest")
    metadata = _read_json(metadata_path, "validation metadata")
    if identity.get("schema") != 1 or metadata.get("schema") != 2:
        raise BuildImageError("unsupported release image metadata schema")

    identity_metadata = identity.get("metadata")
    identity_archive = identity.get("image_archive")
    if not isinstance(identity_metadata, dict) or not isinstance(identity_archive, dict):
        raise BuildImageError("identity manifest artifact bindings are malformed")
    if identity_metadata.get("artifact_path") != "release-image-metadata.json":
        raise BuildImageError("identity manifest names an unexpected metadata artifact")
    if sha256_file(metadata_path) != _require_sha256(identity_metadata.get("sha256"), "metadata SHA-256"):
        raise BuildImageError("downloaded validation metadata digest does not match identity")
    if identity_archive.get("artifact_path") != "release-image.tar":
        raise BuildImageError("identity manifest names an unexpected image archive")
    expected_archive = _require_sha256(identity_archive.get("sha256"), "image archive SHA-256")
    if sha256_file(archive) != expected_archive:
        raise BuildImageError("downloaded image archive digest does not match identity")
    if metadata.get("image_archive_sha256") != expected_archive:
        raise BuildImageError("validation metadata and identity disagree on image archive")

    image_tag = f"{args.image}:{args.version}"
    commit = git_commit(args.repo.resolve())
    for field, actual, expected in (
        ("source commit", metadata.get("source_commit"), commit),
        ("identity source commit", identity.get("source_commit"), commit),
        ("image tag", metadata.get("image_tag"), image_tag),
        ("identity image tag", identity.get("image_tag"), image_tag),
        ("Python base", metadata.get("python_base"), args.python_base),
    ):
        if actual != expected:
            raise BuildImageError(f"{field} does not match publication inputs")

    evidence = metadata.get("evidence")
    identity_evidence = identity.get("evidence")
    if not isinstance(evidence, dict) or not isinstance(identity_evidence, dict):
        raise BuildImageError("evidence bindings are malformed")
    evidence_required = evidence.get("required") is True
    if args.require_evidence and not evidence_required:
        raise BuildImageError("validation did not mark release evidence as mandatory")
    for kind, artifact_path in EVIDENCE_FILES.items():
        entry = evidence.get(kind)
        identity_digest = identity_evidence.get(kind)
        if entry is None:
            if evidence_required or args.require_evidence:
                raise BuildImageError(f"required {kind} evidence is missing")
            if identity_digest is not None:
                raise BuildImageError(f"identity unexpectedly binds absent {kind} evidence")
            continue
        if not isinstance(entry, dict) or entry.get("artifact_path") != artifact_path:
            raise BuildImageError(f"{kind} evidence metadata is malformed")
        digest = _require_sha256(entry.get("sha256"), f"{kind} evidence SHA-256")
        if identity_digest != digest:
            raise BuildImageError(f"{kind} evidence digest does not match identity")
        evidence_file = _verify_artifact_file(artifact_root, artifact_path, kind)
        if sha256_file(evidence_file) != digest:
            raise BuildImageError(f"downloaded {kind} evidence digest does not match identity")

    labels = metadata.get("labels")
    if not isinstance(labels, dict):
        raise BuildImageError("validation metadata labels are malformed")
    expected_labels = {
        "commit": commit,
        "version": args.version,
        "wheel_manifest_sha256": _require_sha256(
            labels.get("wheel_manifest_sha256"), "wheel manifest SHA-256",
        ),
        "oauth_wheel_sha256": _require_sha256(
            labels.get("oauth_wheel_sha256"), "OAuth wheel SHA-256",
        ),
    }
    if labels != expected_labels:
        raise BuildImageError("validation metadata labels do not match publication inputs")
    if load_image:
        subprocess.run(["docker", "image", "load", "--input", str(archive)], check=True)
    image_id, actual_labels, config_sha256 = inspect_image(image_tag, expected_labels)
    expected_image_id = _require_sha256(metadata.get("image_id"), "image ID", prefixed=True)
    expected_config = _require_sha256(metadata.get("config_sha256"), "config SHA-256")
    if image_id != expected_image_id or identity.get("image_id") != image_id:
        raise BuildImageError("loaded image ID does not match validation identity")
    if config_sha256 != expected_config or identity.get("config_sha256") != config_sha256:
        raise BuildImageError("loaded image config does not match validation identity")
    if actual_labels != labels:
        raise BuildImageError("loaded image labels do not match validation metadata")
    return metadata, identity


def _push_digest(image_tag: str) -> str:
    result = subprocess.run(
        ["docker", "push", image_tag], check=True, capture_output=True, text=True,
    )
    matches = PUSH_DIGEST_RE.findall(result.stdout + "\n" + result.stderr)
    if not matches:
        raise BuildImageError("docker push did not report a registry manifest digest")
    return matches[-1]


def remote_manifest_digest(image_tag: str) -> str | None:
    result = subprocess.run(
        ["docker", "buildx", "imagetools", "inspect", image_tag],
        check=False, capture_output=True, text=True,
    )
    if result.returncode != 0:
        message = (result.stdout + "\n" + result.stderr).lower()
        if any(marker in message for marker in ("manifest unknown", "no such manifest", "not found")):
            return None
        raise BuildImageError(f"could not inspect remote tag: {message.strip()}")
    matches = REMOTE_DIGEST_RE.findall(result.stdout)
    if len(matches) != 1:
        raise BuildImageError("remote manifest digest is missing or malformed")
    digest = matches[0]
    return _require_sha256(digest, "remote manifest digest", prefixed=True)


def publish_immutable(image_tag: str, archive_sha256: str) -> tuple[str, bool, str]:
    repository, separator, _version = image_tag.rpartition(":")
    if not separator:
        raise BuildImageError("release image tag is malformed")
    candidate_tag = f"{repository}:candidate-{archive_sha256[:32]}"
    subprocess.run(["docker", "image", "tag", image_tag, candidate_tag], check=True)
    candidate_digest = _push_digest(candidate_tag)
    existing = remote_manifest_digest(image_tag)
    if existing is not None:
        if existing != candidate_digest:
            raise BuildImageError(
                f"release tag already exists at {existing}; candidate is {candidate_digest}"
            )
        return candidate_digest, True, candidate_tag

    existing = remote_manifest_digest(image_tag)
    if existing is not None:
        if existing != candidate_digest:
            raise BuildImageError("release tag changed during immutable-tag check")
        return candidate_digest, True, candidate_tag
    published_digest = _push_digest(image_tag)
    if published_digest != candidate_digest:
        raise BuildImageError("release-tag push digest differs from the staged candidate")
    if remote_manifest_digest(image_tag) != candidate_digest:
        raise BuildImageError("release tag does not resolve to the staged candidate digest")
    return candidate_digest, False, candidate_tag


def publish_bundle(args: argparse.Namespace) -> dict[str, Any]:
    metadata, identity = verify_validation_bundle(args, load_image=False)
    digest, idempotent, candidate_tag = publish_immutable(
        metadata["image_tag"], metadata["image_archive_sha256"],
    )
    publication = {
        "schema": 1,
        "validation_identity_sha256": args.expected_identity_sha256,
        "source_commit": metadata["source_commit"],
        "image_tag": metadata["image_tag"],
        "image_id": metadata["image_id"],
        "config_sha256": metadata["config_sha256"],
        "registry_digest": digest,
        "image_ref": f"{metadata['image_tag']}@{digest}",
        "candidate_tag": candidate_tag,
        "idempotent_existing_tag": idempotent,
        "evidence": identity["evidence"],
    }
    if not args.output:
        raise BuildImageError("publish mode requires a publication metadata output")
    atomic_json(args.output.resolve(), publication)
    return publication


def _validate_common_args(args: argparse.Namespace) -> None:
    if VERSION_RE.fullmatch(args.version) is None:
        raise BuildImageError("version must match <major>.<minor>.<patch>-<sequence>")
    remainder = args.image.removeprefix("ghcr.io/")
    if (remainder == args.image or "/" not in remainder or "@" in args.image
            or ":" in remainder):
        raise BuildImageError("image must be an untagged ghcr.io repository")
    if re.search(r"@sha256:[0-9a-f]{64}$", args.python_base) is None:
        raise BuildImageError("python base must be digest-pinned")
    if args.expected_commit and re.fullmatch(r"[0-9a-f]{40}", args.expected_commit) is None:
        raise BuildImageError("expected commit must be a full commit digest")


def run(args: argparse.Namespace) -> dict[str, Any]:
    _validate_common_args(args)
    if args.mode == "build":
        return create_validation_bundle(args)
    if args.mode == "verify":
        metadata, _identity = verify_validation_bundle(args, load_image=True)
        return metadata
    if args.mode == "publish":
        return publish_bundle(args)
    raise BuildImageError(f"unsupported mode: {args.mode}")


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--mode", choices=("build", "verify", "publish"), default="build")
    value.add_argument("--version", required=True)
    value.add_argument("--image", required=True, help="repository name without tag")
    value.add_argument("--python-base", required=True)
    value.add_argument("--repo", type=Path, default=Path.cwd())
    value.add_argument("--dockerfile", type=Path, default=Path("deploy/Dockerfile"))
    value.add_argument("--wheelhouse", type=Path, default=Path("deploy/wheelhouse"))
    value.add_argument("--expected-commit")
    value.add_argument("--output", type=Path)
    value.add_argument("--metadata", type=Path)
    value.add_argument("--identity", type=Path)
    value.add_argument("--image-archive", type=Path)
    value.add_argument("--expected-identity-sha256")
    value.add_argument("--sbom")
    value.add_argument("--scan")
    value.add_argument("--require-evidence", action="store_true")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    try:
        metadata = run(parser().parse_args(argv))
    except (BuildImageError, OSError, subprocess.CalledProcessError, ValueError) as exc:
        print(f"release image operation failed: {exc}")
        return 1
    print(json.dumps(metadata, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
