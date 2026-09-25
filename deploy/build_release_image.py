#!/usr/bin/env python3
"""Build, seal, verify, and publish one release-labelled Menhir image."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import secrets
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

# Keep the sibling parts package importable even when this module is loaded by
# path (importlib spec) rather than imported as a script or package member.
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.append(str(_SCRIPT_DIR))

from build_release_image_parts import (  # noqa: E402
    CANONICAL_SOURCE_REPOSITORY, BuildImageError, DIGEST_RE, EVIDENCE_FILES, LABELS,
    MAX_EVIDENCE_BYTES, PUSH_DIGEST_RE, REMOTE_DIGEST_RE, SCANNER_REPOSITORIES, SEVERITIES,
    SHA256_RE, VERSION_RE, atomic_json, build_command, committed_build_context,
    git_source_date_epoch, inspect_image, oauth_wheel_sha256, parser, remote_manifest_digest,
    sha256_bytes, sha256_file, _parse_report, _push_digest, _read_bytes, _read_json_bytes,
    _repository_relative, _require_sha256, _scanner_descriptor, _scanner_source_image_id,
    _validate_common_args, _validate_scanner_image, _verify_artifact_file, _write_evidence,
)


def git_commit(repo: Path, *, allow_untracked: bool = False) -> str:
    for args, label in [(["diff", "--quiet"], "tracked worktree"),
                        (["diff", "--cached", "--quiet"], "index")]:
        result = subprocess.run(["git", "-C", str(repo), *args], check=False)
        if result.returncode != 0:
            raise BuildImageError(f"Git {label} is not clean")
    if not allow_untracked:
        result = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain=v1", "--untracked-files=all"],
            check=True, capture_output=True, text=True,
        )
        if result.stdout:
            raise BuildImageError("Git worktree contains untracked files")
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    )
    commit = result.stdout.strip()
    if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise BuildImageError("Git HEAD is not a full commit digest")
    return commit


def validate_sbom(raw: bytes, expected_image_id: str) -> dict[str, Any]:
    document = _parse_report(raw, "syft")
    version = _scanner_descriptor(document, "syft")
    source_image_id = _scanner_source_image_id(document, "syft", expected_image_id)
    artifacts = document.get("artifacts")
    relationships = document.get("artifactRelationships")
    schema = document.get("schema")
    if (not isinstance(artifacts, list) or not isinstance(relationships, list)
            or not isinstance(schema, dict)):
        raise BuildImageError("Syft SBOM structure is malformed")
    if not artifacts:
        raise BuildImageError("Syft SBOM contains no packages")
    schema_url = schema.get("url")
    if (not isinstance(schema.get("version"), str)
            or not isinstance(schema_url, str)
            or "anchore/syft" not in schema_url):
        raise BuildImageError("Syft SBOM schema identity is missing")
    return {
        "format": "syft-json",
        "scanner_version": version,
        "source_image_id": source_image_id,
        "package_count": len(artifacts),
    }


def validate_vulnerability_scan(raw: bytes, expected_image_id: str) -> dict[str, Any]:
    document = _parse_report(raw, "grype")
    version = _scanner_descriptor(document, "grype")
    source_image_id = _scanner_source_image_id(document, "grype", expected_image_id)
    descriptor = document["descriptor"]
    database = descriptor.get("db")
    if not isinstance(database, dict) or not database:
        raise BuildImageError("Grype report has no vulnerability database identity")
    counts = {severity: 0 for severity in SEVERITIES}
    for field in ("matches", "ignoredMatches"):
        matches = document.get(field, [] if field == "ignoredMatches" else None)
        if not isinstance(matches, list):
            raise BuildImageError(f"Grype report {field} is malformed")
        for number, match in enumerate(matches, 1):
            vulnerability = match.get("vulnerability") if isinstance(match, dict) else None
            severity = vulnerability.get("severity") if isinstance(vulnerability, dict) else None
            if severity not in counts:
                raise BuildImageError(
                    f"Grype report {field} entry {number} has an invalid severity"
                )
            counts[severity] += 1
    if counts["Critical"]:
        raise BuildImageError(
            f"vulnerability policy rejected {counts['Critical']} critical finding(s)"
        )
    return {
        "format": "grype-json",
        "scanner_version": version,
        "source_image_id": source_image_id,
        "database_sha256": hashlib.sha256(json.dumps(
            database, ensure_ascii=True, separators=(",", ":"), sort_keys=True,
        ).encode("ascii")).hexdigest(),
        "severity_counts": counts,
        "policy": {"maximum_allowed_severity": "High", "passed": True},
    }


def _run_scanner(*, scanner: str, image: str, archive: Path) -> bytes:
    mount = f"type=bind,src={archive.parent},dst=/candidate,readonly"
    command = [
        "docker", "run", "--rm", "--pull", "never", "--read-only",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges=true",
        "--mount", mount,
        "--env", "HOME=/tmp",
    ]
    database_volume: str | None = None
    if scanner == "syft":
        command.extend((
            "--tmpfs", "/tmp:rw,nosuid,nodev,size=268435456",
            "--network", "none", "--env", "SYFT_CHECK_FOR_APP_UPDATE=false",
        ))
    else:
        database_volume = "menhir-grype-db-" + secrets.token_hex(12)
        subprocess.run(
            ["docker", "volume", "create", "--name", database_volume],
            check=True, capture_output=True,
        )
        command.extend((
            "--mount", f"type=volume,src={database_volume},dst=/tmp",
            "--env", "GRYPE_DB_CACHE_DIR=/tmp/grype/db",
            "--env", "GRYPE_CHECK_FOR_APP_UPDATE=false",
        ))
    command.extend((
        image, f"docker-archive:/candidate/{archive.name}",
        "--output", "syft-json" if scanner == "syft" else "json",
    ))
    try:
        completed = subprocess.run(command, check=False, capture_output=True)
        if completed.returncode != 0:
            error = completed.stderr.decode("utf-8", errors="replace").strip()
            raise BuildImageError(f"{scanner} scanner failed: {error[-2000:]}")
        return completed.stdout
    finally:
        if database_volume is not None:
            subprocess.run(
                ["docker", "volume", "rm", "--force", database_volume],
                check=False, capture_output=True,
            )


def generate_evidence(*, artifact_root: Path, archive: Path, archive_sha256: str,
                      image_id: str, syft_image: str,
                      grype_image: str) -> dict[str, Any]:
    result: dict[str, Any] = {"required": True}
    specifications = (
        ("sbom", "syft", syft_image, validate_sbom),
        ("vulnerability_scan", "grype", grype_image, validate_vulnerability_scan),
    )
    for kind, scanner, scanner_image, validator in specifications:
        raw = _run_scanner(scanner=scanner, image=scanner_image, archive=archive)
        summary = validator(raw, image_id)
        artifact_path = EVIDENCE_FILES[kind]
        result[kind] = {
            "artifact_path": artifact_path,
            "sha256": _write_evidence(artifact_root / artifact_path, raw),
            "scanner_image": scanner_image,
            "subject": {
                "image_id": image_id,
                "image_archive_sha256": archive_sha256,
            },
            "validation": summary,
        }
    return result


def create_validation_bundle(args: argparse.Namespace) -> dict[str, Any]:
    repo = args.repo.resolve()
    dockerfile = (repo / args.dockerfile).resolve()
    wheelhouse = (repo / args.wheelhouse).resolve()
    syft_image = _validate_scanner_image(args.syft_image, "syft")
    grype_image = _validate_scanner_image(args.grype_image, "grype")
    commit = git_commit(repo, allow_untracked=True)
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
    source_date_epoch = git_source_date_epoch(repo, commit)
    with committed_build_context(repo, commit, dockerfile, wheelhouse) as (
        build_repo, build_dockerfile,
    ):
        subprocess.run(build_command(
            repo=build_repo, dockerfile=build_dockerfile, image_tag=image_tag,
            python_base=args.python_base, commit=commit, version=args.version,
            wheel_manifest=wheel_manifest, oauth_wheel=oauth_wheel,
            source_date_epoch=source_date_epoch,
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
    evidence = generate_evidence(
        artifact_root=artifact_root, archive=archive,
        archive_sha256=archive_sha256, image_id=image_id,
        syft_image=syft_image, grype_image=grype_image,
    )
    metadata = {
        "schema": 3,
        "source_repository": args.source_repository,
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
        "schema": 2,
        "source_repository": args.source_repository,
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
    identity_raw = _read_bytes(identity_path, "identity manifest")
    metadata_raw = _read_bytes(metadata_path, "validation metadata")
    if sha256_bytes(identity_raw) != expected_identity:
        raise BuildImageError("downloaded identity manifest digest does not match validation output")
    identity = _read_json_bytes(identity_raw, "identity manifest")
    metadata = _read_json_bytes(metadata_raw, "validation metadata")
    if identity.get("schema") != 2 or metadata.get("schema") != 3:
        raise BuildImageError("unsupported release image metadata schema")

    identity_metadata = identity.get("metadata")
    identity_archive = identity.get("image_archive")
    if not isinstance(identity_metadata, dict) or not isinstance(identity_archive, dict):
        raise BuildImageError("identity manifest artifact bindings are malformed")
    if identity_metadata.get("artifact_path") != "release-image-metadata.json":
        raise BuildImageError("identity manifest names an unexpected metadata artifact")
    if sha256_bytes(metadata_raw) != _require_sha256(identity_metadata.get("sha256"), "metadata SHA-256"):
        raise BuildImageError("downloaded validation metadata digest does not match identity")
    if identity_archive.get("artifact_path") != "release-image.tar":
        raise BuildImageError("identity manifest names an unexpected image archive")
    expected_archive = _require_sha256(identity_archive.get("sha256"), "image archive SHA-256")
    if sha256_file(archive) != expected_archive:
        raise BuildImageError("downloaded image archive digest does not match identity")
    if metadata.get("image_archive_sha256") != expected_archive:
        raise BuildImageError("validation metadata and identity disagree on image archive")

    image_tag = f"{args.image}:{args.version}"
    commit = git_commit(args.repo.resolve(), allow_untracked=True)
    for field, actual, expected in (
        ("source repository", metadata.get("source_repository"), args.source_repository),
        ("identity source repository", identity.get("source_repository"), args.source_repository),
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
    if set(evidence) != {"required", *EVIDENCE_FILES}:
        raise BuildImageError("validation metadata contains unexpected evidence fields")
    if set(identity_evidence) != set(EVIDENCE_FILES):
        raise BuildImageError("identity manifest contains unexpected evidence fields")
    if evidence.get("required") is not True:
        raise BuildImageError("validation did not mark release evidence as mandatory")
    validators = {
        "sbom": ("syft", validate_sbom),
        "vulnerability_scan": ("grype", validate_vulnerability_scan),
    }
    for kind, artifact_path in EVIDENCE_FILES.items():
        entry = evidence.get(kind)
        identity_digest = identity_evidence.get(kind)
        if not isinstance(entry, dict) or entry.get("artifact_path") != artifact_path:
            raise BuildImageError(f"{kind} evidence metadata is malformed")
        digest = _require_sha256(entry.get("sha256"), f"{kind} evidence SHA-256")
        if identity_digest != digest:
            raise BuildImageError(f"{kind} evidence digest does not match identity")
        evidence_file = _verify_artifact_file(artifact_root, artifact_path, kind)
        evidence_raw = _read_bytes(evidence_file, f"{kind} evidence")
        if sha256_bytes(evidence_raw) != digest:
            raise BuildImageError(f"downloaded {kind} evidence digest does not match identity")
        scanner, validator = validators[kind]
        _validate_scanner_image(entry.get("scanner_image"), scanner)
        expected_subject = {
            "image_id": metadata.get("image_id"),
            "image_archive_sha256": expected_archive,
        }
        if entry.get("subject") != expected_subject:
            raise BuildImageError(f"{kind} evidence names the wrong candidate subject")
        validation = validator(evidence_raw, expected_subject["image_id"])
        if entry.get("validation") != validation:
            raise BuildImageError(f"{kind} parsed validation does not match its report")

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


def _verify_remote_candidate(
    *, repository: str, digest: str, metadata: dict[str, Any],
) -> None:
    digest_ref = f"{repository}@{digest}"
    subprocess.run(["docker", "pull", digest_ref], check=True)
    remote_id, remote_labels, remote_config = inspect_image(
        digest_ref, metadata["labels"],
    )
    if remote_id != metadata["image_id"]:
        raise BuildImageError("registry candidate image ID differs from the sealed candidate")
    if remote_config != metadata["config_sha256"]:
        raise BuildImageError("registry candidate config differs from the sealed candidate")
    if remote_labels != metadata["labels"]:
        raise BuildImageError("registry candidate labels differ from the sealed candidate")
    if remote_manifest_digest(digest_ref) != digest:
        raise BuildImageError("digest-pinned registry candidate did not verify")


def publish_candidate(metadata: dict[str, Any]) -> tuple[str, bool, str]:
    image_tag = metadata["image_tag"]
    repository, separator, _version = image_tag.rpartition(":")
    if not separator:
        raise BuildImageError("release image tag is malformed")
    candidate_tag = f"{repository}:candidate-{metadata['image_archive_sha256']}"
    subprocess.run(["docker", "image", "tag", image_tag, candidate_tag], check=True)
    existing = remote_manifest_digest(candidate_tag)
    if existing is not None:
        _verify_remote_candidate(
            repository=repository, digest=existing, metadata=metadata,
        )
        return existing, True, candidate_tag

    existing = remote_manifest_digest(candidate_tag)
    if existing is not None:
        _verify_remote_candidate(
            repository=repository, digest=existing, metadata=metadata,
        )
        return existing, True, candidate_tag
    published_digest = _push_digest(candidate_tag)
    if remote_manifest_digest(candidate_tag) != published_digest:
        raise BuildImageError("candidate tag changed during registry verification")
    _verify_remote_candidate(
        repository=repository, digest=published_digest, metadata=metadata,
    )
    return published_digest, False, candidate_tag


def publish_bundle(args: argparse.Namespace) -> dict[str, Any]:
    metadata, identity = verify_validation_bundle(args, load_image=True)
    digest, idempotent, candidate_tag = publish_candidate(metadata)
    repository = metadata["image_tag"].rpartition(":")[0]
    publication = {
        "schema": 2,
        "source_repository": metadata["source_repository"],
        "validation_identity_sha256": args.expected_identity_sha256,
        "source_commit": metadata["source_commit"],
        "image_tag": metadata["image_tag"],
        "image_id": metadata["image_id"],
        "config_sha256": metadata["config_sha256"],
        "image_archive_sha256": metadata["image_archive_sha256"],
        "registry_digest": digest,
        "image_ref": f"{repository}@{digest}",
        "candidate_tag": candidate_tag,
        "candidate_ref": f"{candidate_tag}@{digest}",
        "idempotent_existing_candidate": idempotent,
        "evidence": identity["evidence"],
    }
    if not args.output:
        raise BuildImageError("publish mode requires a publication metadata output")
    atomic_json(args.output.resolve(), publication)
    return publication


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
