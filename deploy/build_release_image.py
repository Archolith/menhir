#!/usr/bin/env python3
"""Build, seal, verify, and publish one release-labelled Menhir image."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
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
    "sbom": "release-image-evidence/sbom.syft.json",
    "vulnerability_scan": "release-image-evidence/scan.grype.json",
}
SCANNER_REPOSITORIES = {
    "syft": frozenset({"anchore/syft", "docker.io/anchore/syft"}),
    "grype": frozenset({"anchore/grype", "docker.io/anchore/grype"}),
}
SEVERITIES = ("Unknown", "Negligible", "Low", "Medium", "High", "Critical")
MAX_EVIDENCE_BYTES = 256 * 1024 * 1024


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


def _validate_scanner_image(value: Any, scanner: str) -> str:
    if not isinstance(value, str):
        raise BuildImageError(f"{scanner} scanner image is required")
    repository, separator, digest = value.rpartition("@")
    if (not separator or repository not in SCANNER_REPOSITORIES[scanner]
            or DIGEST_RE.fullmatch(digest) is None):
        raise BuildImageError(
            f"{scanner} scanner must use its official image pinned by SHA-256 digest"
        )
    return value


def _scanner_descriptor(document: dict[str, Any], scanner: str) -> str:
    descriptor = document.get("descriptor")
    if not isinstance(descriptor, dict):
        raise BuildImageError(f"{scanner} report descriptor is missing")
    name = descriptor.get("name")
    version = descriptor.get("version")
    if not isinstance(name, str) or name.lower() != scanner:
        raise BuildImageError(f"{scanner} report names an unexpected scanner")
    if not isinstance(version, str) or not version or len(version) > 128:
        raise BuildImageError(f"{scanner} report version is missing or malformed")
    return version


def _scanner_source_image_id(
    document: dict[str, Any], scanner: str, expected_image_id: str,
) -> str:
    source = document.get("source")
    if not isinstance(source, dict) or source.get("type") != "image":
        raise BuildImageError(f"{scanner} report source is not a container image")
    candidates: list[Any] = []
    for field in ("metadata", "target"):
        nested = source.get(field)
        if isinstance(nested, dict):
            candidates.extend((nested.get("id"), nested.get("imageID")))
    candidates.extend((source.get("imageID"), source.get("id")))
    image_ids = {
        value for value in candidates
        if isinstance(value, str) and DIGEST_RE.fullmatch(value) is not None
    }
    if expected_image_id not in image_ids:
        raise BuildImageError(
            f"{scanner} report does not identify the expected candidate image"
        )
    return expected_image_id


def _parse_report(raw: bytes, scanner: str) -> dict[str, Any]:
    if not raw or len(raw) > MAX_EVIDENCE_BYTES:
        raise BuildImageError(f"{scanner} report is empty or too large")
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BuildImageError(f"{scanner} report is not valid UTF-8 JSON") from exc
    if not isinstance(document, dict):
        raise BuildImageError(f"{scanner} report must contain a JSON object")
    return document


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


def _write_evidence(path: Path, raw: bytes) -> str:
    if path.parent.is_symlink():
        raise BuildImageError("evidence destination must not traverse a symlink")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        raise BuildImageError(f"evidence destination already exists: {path}")
    with path.open("xb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    return sha256_file(path)


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
    current = root
    for part in Path(relative).parts:
        current = current / part
        if current.is_symlink():
            raise BuildImageError(f"{label} must not traverse a symlink")
    try:
        candidate = current.resolve(strict=True)
        candidate.relative_to(root)
    except (FileNotFoundError, ValueError):
        raise BuildImageError(f"{label} escaped the validation artifact") from None
    if not candidate.is_file() or candidate.is_symlink():
        raise BuildImageError(f"{label} is missing or unsafe")
    return candidate


def create_validation_bundle(args: argparse.Namespace) -> dict[str, Any]:
    repo = args.repo.resolve()
    dockerfile = (repo / args.dockerfile).resolve()
    wheelhouse = (repo / args.wheelhouse).resolve()
    syft_image = _validate_scanner_image(args.syft_image, "syft")
    grype_image = _validate_scanner_image(args.grype_image, "grype")
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
    evidence = generate_evidence(
        artifact_root=artifact_root, archive=archive,
        archive_sha256=archive_sha256, image_id=image_id,
        syft_image=syft_image, grype_image=grype_image,
    )
    metadata = {
        "schema": 3,
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
    if identity.get("schema") != 2 or metadata.get("schema") != 3:
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
        if sha256_file(evidence_file) != digest:
            raise BuildImageError(f"downloaded {kind} evidence digest does not match identity")
        scanner, validator = validators[kind]
        _validate_scanner_image(entry.get("scanner_image"), scanner)
        expected_subject = {
            "image_id": metadata.get("image_id"),
            "image_archive_sha256": expected_archive,
        }
        if entry.get("subject") != expected_subject:
            raise BuildImageError(f"{kind} evidence names the wrong candidate subject")
        validation = validator(evidence_file.read_bytes(), expected_subject["image_id"])
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
    value.add_argument("--syft-image")
    value.add_argument("--grype-image")
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
