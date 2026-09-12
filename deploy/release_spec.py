#!/usr/bin/env python3
"""Prepare strict, reproducible inputs for release-author.py."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

import menhir_schema  # noqa: E402
import verify_wheelhouse  # noqa: E402
import build_release_image  # noqa: E402


class ReleaseSpecError(ValueError):
    """A release preparation input or derived value is invalid."""


REPOSITORIES = frozenset({"menhir", "archolith_oauth", "yawn_deploy", "yawn_vps"})
IMAGES = frozenset({"menhir", "neo4j", "caddy", "base"})
SECRET_VERSIONS = frozenset({
    "neo4j-auth", "neo4j-password", "oauth-signing-key",
    "oauth-retry-keyring", "oauth-consent-secret", "operator-key",
    "client-policy", "provider-key",
})
INPUT_KEYS = frozenset({
    "schema", "release_id", "release_author", "release_workspace_root",
    "repositories", "images", "evidence", "baseline_production_env",
    "operations_policy", "oauth_public_key", "python_runtime_digest",
    "prior_release", "prior_route", "secret_version_ids", "yawn_env_sha256",
    "ingress_mode",
})
# Optional. Absent means "rebuilt", so every input written before this existed
# keeps its exact previous meaning and the rebuilt contract is untouched.
INPUT_KEYS_WITH_PROVENANCE = INPUT_KEYS | frozenset({"image_provenance"})
# A release that rebuilds the image must supply the full publication and
# attestation chain for the bytes it produced.
EVIDENCE_KEYS = frozenset({
    "wheelhouse", "sbom", "scan", "image_publication",
    "image_metadata", "image_identity", "image_archive",
    "publication_attestation", "attestation_trusted_root",
})
# A release that ships the prior release's image unchanged cannot supply that
# chain: nothing was built, so there is nothing new to attest. It inherits the
# binding instead, and must prove the image digests are identical to the prior
# release authority. The resulting record carries no image_publication or
# image_refs, which is the _PRE_IMAGE_PUBLICATION release shape the schema
# already accepts.
#
# This is deliberately not a way to skip attestation. An inherited release is
# attested exactly as strongly as the release it inherits from, and claims
# nothing more: it asserts only that these are the same bytes that release
# bound. Rebuilding is still required whenever the image changes.
INHERITED_EVIDENCE_KEYS = frozenset({"wheelhouse", "sbom", "scan"})
IMAGE_PROVENANCE_VALUES = frozenset({"rebuilt", "inherited"})
IMAGE_KEYS = frozenset({"digest", "ref"})
PUBLICATION_KEYS = frozenset({
    "schema", "source_repository", "validation_identity_sha256",
    "source_commit", "image_tag",
    "image_id", "config_sha256", "image_archive_sha256",
    "registry_digest", "image_ref", "candidate_tag", "candidate_ref",
    "idempotent_existing_candidate", "evidence",
})
IMAGE_METADATA_KEYS = frozenset({
    "schema", "source_repository", "source_commit", "image_tag", "image_id", "config_sha256",
    "image_archive_sha256", "python_base", "labels", "evidence",
})
IMAGE_IDENTITY_KEYS = frozenset({
    "schema", "source_repository", "source_commit", "image_tag", "image_id", "config_sha256",
    "image_archive", "metadata", "evidence",
})
IMAGE_LABEL_KEYS = frozenset({
    "commit", "version", "wheel_manifest_sha256", "oauth_wheel_sha256",
})
IMAGE_EVIDENCE_KEYS = frozenset({"required", "sbom", "vulnerability_scan"})
IMAGE_EVIDENCE_ENTRY_KEYS = frozenset({
    "artifact_path", "sha256", "scanner_image", "subject", "validation",
})
IMAGE_EVIDENCE_DIGEST_KEYS = frozenset({"sbom", "vulnerability_scan"})
IMAGE_ARTIFACT_BINDING_KEYS = frozenset({"artifact_path", "sha256"})
IMAGE_SUBJECT_KEYS = frozenset({"image_id", "image_archive_sha256"})
OPERATIONS_POLICY_KEYS = frozenset({
    "schema", "issuer", "audience", "base_url", "clients",
})
OPERATIONS_CLIENT_KEYS = frozenset({"tier", "scopes", "tools"})
OPERATIONS_POLICY_TOOLS = frozenset({
    "menhir_release_inspect",
    "menhir_status",
    "menhir_logs",
    "menhir_backup_status",
    "menhir_generation_inspect",
})
RELEASE_ID_RE = re.compile(r"^menhir-prod-[0-9]+\.[0-9]+\.[0-9]+-[0-9]+$")
AUTHOR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@+-]{0,127}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,255}$")
ENV_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
IMAGE_REF_RE = re.compile(
    r"^[a-z0-9]+(?:[._-][a-z0-9]+)*(?::[0-9]+)?"
    r"(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*"
    r"(?::[A-Za-z0-9][A-Za-z0-9._-]{0,127})?"
    r"@sha256:[0-9a-f]{64}$"
)
PLACEHOLDER_RE = re.compile(
    r"<[^>]+>|\{\{[^}]+\}\}|\b(?:TODO|CHANGEME|REPLACE_WITH)\b", re.I
)
SECRET_KEY_RE = re.compile(
    r"(?:^|_)(?:PASSWORD|PASSWD|SECRET|TOKEN|API_KEY|PRIVATE_KEY|CREDENTIAL)(?:$|_)",
    re.I,
)
SECRET_VALUE_RE = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|"
    r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}|"
    r"\b(?:sk|ghp|github_pat)_[A-Za-z0-9_-]{16,}",
    re.I,
)
CANONICAL_GITHUB_REPOSITORY = "Archolith/menhir"
REVIEWED_GITHUB_ATTESTATION_TRUSTED_ROOT_SHA256 = (
    "65ca537f6ed8a47fd0e560c421baa1f6c1efb8b25fc200d8c5c02c0e92eb2b9c"
)


def _git(repository: str, path: str) -> dict[str, str]:
    return {"kind": "git", "repository": repository, "path": path}


def _rendered(key: str) -> dict[str, str]:
    return {"kind": "rendered", "rendered_key": key}


# The sole destination-to-source authority for the installed artifact census.
ARTIFACT_SOURCES: dict[str, dict[str, str]] = {
    "/etc/yawn-vps/menhir-oauth-policy.json": _rendered(
        "operations_policy_sha256"
    ),
    "/etc/yawn-vps/menhir-oauth-public.pem": _rendered(
        "oauth_public_key_sha256"
    ),
    "/etc/yawn-vps/menhir-python-runtime.sha256": _rendered(
        "python_runtime_digest_sha256"
    ),
    "/srv/menhir/production/release/production.env": _rendered(
        "production_env_sha256"
    ),
    "/srv/menhir/production/policy/client-policy.json": _git(
        "menhir", "deploy/client-policy.production.json"
    ),
    "/srv/menhir/production/deploy/Dockerfile": _git(
        "menhir", "deploy/Dockerfile"
    ),
    "/srv/menhir/production/deploy/docker-compose.production.yml": _git(
        "menhir", "deploy/docker-compose.production.yml"
    ),
    "/srv/menhir/production/deploy/durable-state-inventory.json": _git(
        "menhir", "deploy/durable-state-inventory.json"
    ),
    "/srv/menhir/production/deploy/installed-artifacts.json": _git(
        "menhir", "deploy/installed-artifacts.json"
    ),
    # The live Cloudflared ingress. Sole route to memory.ctharvey.me; previously
    # hand-placed on the host, in no release and no backup. Sourced from deploy/
    # rather than pipeline/ so there is exactly one blob per file.
    "/srv/menhir/production/ingress/cloudflared-config.yml": _git(
        "menhir", "deploy/cloudflared-config.yml"
    ),
    "/srv/menhir/production/ingress/docker-compose.cloudflared.yml": _git(
        "menhir", "deploy/docker-compose.cloudflared.yml"
    ),
    "/srv/yawn/projects/yawn.vps/menhir_server.py": _git(
        "yawn_vps", "menhir_server.py"
    ),
    "/srv/yawn/projects/yawn.vps/vps/core.py": _git("yawn_vps", "vps/core.py"),
    "/srv/yawn/projects/yawn.vps/vps/menhir_capabilities.py": _git(
        "yawn_vps", "vps/menhir_capabilities.py"
    ),
    "/srv/yawn/projects/yawn.vps/vps/menhir_tools.py": _git(
        "yawn_vps", "vps/menhir_tools.py"
    ),
    "/srv/yawn/projects/yawn.vps/vps/oauth_policy.py": _git(
        "yawn_vps", "vps/oauth_policy.py"
    ),
    "/usr/local/sbin/menhir-backup-local": _git(
        "menhir", "deploy/menhir-backup-local.sh"
    ),
}
for _name in (
    "backup-status", "generation-inspect", "lib.sh", "logs", "recover",
    "release-inspect", "status", "verify-artifacts",
):
    ARTIFACT_SOURCES[f"/srv/menhir/production/bin/{_name}"] = _git(
        "yawn_vps", f"ops/menhir/bin/{_name}"
    )
for _name in (
    "backup-generation.sh", "candidate-accept.sh", "candidate-deploy.sh",
    "promote.sh", "release-lib.sh", "release-run.sh", "release-validate.sh",
    "restore-generation.sh", "rollback.sh", "same-host-fence.sh",
    "secrets-map.sh", "stage-generation.sh",
):
    ARTIFACT_SOURCES[f"/srv/menhir/production/bin/{_name}"] = _git(
        "menhir", f"deploy/{_name}"
    )
for _name in (
    "authority_digest.py", "backup_cleanup_txn.py", "make_manifest.py",
    "mcp_acceptance_probe.py", "menhir_schema.py", "restore_authority_txn.py",
    "same_host_fence.py", "stage_generation.py",
    "validate_durable_inventory.py",
):
    ARTIFACT_SOURCES[f"/srv/menhir/production/bin/{_name}"] = _git(
        "menhir", f"deploy/lib/{_name}"
    )
ARTIFACT_SOURCES["/srv/menhir/production/bin/verify_python_runtime.py"] = _git(
    "yawn_vps", "ops/menhir/bin/verify_python_runtime.py"
)
for _name in ("menhir-oauth-operations.service",):
    ARTIFACT_SOURCES[f"/etc/systemd/system/{_name}"] = _git(
        "yawn_vps", f"ops/menhir/systemd/{_name}"
    )
ARTIFACT_SOURCES["/etc/sudoers.d/menhir-production"] = _git(
    "yawn_vps", "ops/menhir/etc/sudoers.d/menhir-production"
)
ARTIFACT_SOURCES["/etc/tmpfiles.d/menhir-production.conf"] = _git(
    "yawn_vps", "ops/menhir/etc/tmpfiles.d/menhir-production.conf"
)


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ReleaseSpecError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_json(path: Path, label: str) -> dict[str, Any]:
    path = _regular(path, label)
    return _load_json_bytes(path.read_bytes(), label)


def _load_json_bytes(data: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            data.decode("utf-8"), object_pairs_hook=_unique_pairs
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseSpecError(f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ReleaseSpecError(f"{label} must be a JSON object")
    return value


def _exact(value: Any, keys: frozenset[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ReleaseSpecError(f"{label} must be an object")
    missing = sorted(keys - set(value))
    extra = sorted(set(value) - keys)
    if missing or extra:
        raise ReleaseSpecError(
            f"{label} keys mismatch: missing={missing}, extra={extra}"
        )
    return value


def _absolute(path_value: Any, label: str) -> Path:
    if not isinstance(path_value, str) or not path_value:
        raise ReleaseSpecError(f"{label} must be a path string")
    path = Path(path_value)
    if not path.is_absolute():
        raise ReleaseSpecError(f"{label} must be absolute")
    return path


def _regular(path_value: Any, label: str) -> Path:
    path = path_value if isinstance(path_value, Path) else _absolute(path_value, label)
    try:
        resolved = path.resolve(strict=True)
        path.lstat()
    except OSError as exc:
        raise ReleaseSpecError(f"{label} does not exist: {path}") from exc
    if path.is_symlink() or not path.is_file() or (
        os.path.normcase(str(path)) != os.path.normcase(str(resolved))
    ):
        raise ReleaseSpecError(f"{label} must be a regular non-symlink file")
    return resolved


def _directory(path_value: Any, label: str) -> Path:
    path = path_value if isinstance(path_value, Path) else _absolute(path_value, label)
    try:
        resolved = path.resolve(strict=True)
        path.lstat()
    except OSError as exc:
        raise ReleaseSpecError(f"{label} does not exist: {path}") from exc
    if path.is_symlink() or not path.is_dir() or (
        os.path.normcase(str(path)) != os.path.normcase(str(resolved))
    ):
        raise ReleaseSpecError(f"{label} must be an absolute non-symlink directory")
    return resolved


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _capture_bytes(path: Path, label: str) -> bytes:
    return _regular(path, label).read_bytes()


def _verify_github_attestation(
    *, subject: bytes, bundle: bytes, trusted_root: bytes,
    repository: str, source_commit: str,
) -> None:
    """Cryptographically verify captured publication bytes without network lookup."""
    if (
        _sha256_bytes(trusted_root)
        != REVIEWED_GITHUB_ATTESTATION_TRUSTED_ROOT_SHA256
    ):
        raise ReleaseSpecError(
            "GitHub attestation trusted root does not match reviewed authority"
        )
    with tempfile.TemporaryDirectory(prefix="menhir-attestation-") as temporary:
        root = Path(temporary)
        subject_path = root / "release-image-publication.json"
        bundle_path = root / "publication-attestation.json"
        trusted_root_path = root / "trusted-root.jsonl"
        subject_path.write_bytes(subject)
        bundle_path.write_bytes(bundle)
        trusted_root_path.write_bytes(trusted_root)
        command = [
            "gh", "attestation", "verify", str(subject_path),
            "--repo", repository,
            "--bundle", str(bundle_path),
            "--custom-trusted-root", str(trusted_root_path),
            "--source-digest", source_commit,
            "--signer-workflow", f"{repository}/.github/workflows/release-image.yml",
            "--deny-self-hosted-runners",
            "--format", "json",
        ]
        try:
            subprocess.run(command, check=True, capture_output=True, text=True)
        except (OSError, subprocess.CalledProcessError) as exc:
            raise ReleaseSpecError(
                "GitHub publication attestation verification failed"
            ) from exc


def _require_sha256(value: Any, label: str, *, prefixed: bool = False) -> str:
    pattern = DIGEST_RE if prefixed else SHA256_RE
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        suffix = "sha256:<64 lowercase hex>" if prefixed else "64-char lowercase sha256"
        raise ReleaseSpecError(f"{label} must be a {suffix}")
    return value


def validate_image_publication(
    *,
    publication_path: Path,
    metadata_path: Path,
    identity_path: Path,
    archive_path: Path,
    sbom_path: Path,
    scan_path: Path,
    publication_attestation_path: Path,
    attestation_trusted_root_path: Path,
    menhir_commit: str,
    menhir_digest: str,
    menhir_ref: str,
    base_ref: str,
    release_id: str,
    wheel_manifest_sha256: str,
    oauth_wheel_sha256: str,
) -> dict[str, str]:
    """Revalidate the complete CI image publication chain for release authoring."""
    publication_raw = _capture_bytes(publication_path, "image publication")
    metadata_raw = _capture_bytes(metadata_path, "image validation metadata")
    identity_raw = _capture_bytes(identity_path, "image validation identity")
    sbom_raw = _capture_bytes(sbom_path, "SBOM evidence")
    scan_raw = _capture_bytes(scan_path, "vulnerability scan evidence")
    attestation_raw = _capture_bytes(
        publication_attestation_path, "publication attestation bundle"
    )
    trusted_root_raw = _capture_bytes(
        attestation_trusted_root_path, "attestation trusted root"
    )
    publication = _exact(
        _load_json_bytes(publication_raw, "image publication"),
        PUBLICATION_KEYS,
        "image publication",
    )
    metadata = _exact(
        _load_json_bytes(metadata_raw, "image validation metadata"),
        IMAGE_METADATA_KEYS,
        "image validation metadata",
    )
    identity = _exact(
        _load_json_bytes(identity_raw, "image validation identity"),
        IMAGE_IDENTITY_KEYS,
        "image validation identity",
    )
    if publication.get("schema") != 2:
        raise ReleaseSpecError("image publication schema must be 2")
    if metadata.get("schema") != 3:
        raise ReleaseSpecError("image validation metadata schema must be 3")
    if identity.get("schema") != 2:
        raise ReleaseSpecError("image validation identity schema must be 2")

    identity_sha = _sha256_bytes(identity_raw)
    if publication.get("validation_identity_sha256") != identity_sha:
        raise ReleaseSpecError(
            "image publication does not bind the validation identity"
        )
    metadata_binding = _exact(
        identity.get("metadata"),
        IMAGE_ARTIFACT_BINDING_KEYS,
        "image validation identity metadata",
    )
    if metadata_binding != {
        "artifact_path": "release-image-metadata.json",
        "sha256": _sha256_bytes(metadata_raw),
    }:
        raise ReleaseSpecError(
            "image validation identity does not bind the validation metadata"
        )

    archive_sha = _sha256(archive_path)
    archive_binding = _exact(
        identity.get("image_archive"),
        IMAGE_ARTIFACT_BINDING_KEYS,
        "image validation identity archive",
    )
    if archive_binding != {
        "artifact_path": "release-image.tar",
        "sha256": archive_sha,
    }:
        raise ReleaseSpecError(
            "image validation identity does not bind the image archive"
        )

    version = release_id.removeprefix("menhir-prod-")
    repository, separator, reference_digest = menhir_ref.rpartition("@")
    expected_tag = f"{repository}:{version}"
    expected_candidate_tag = f"{repository}:candidate-{archive_sha}"
    if not separator or reference_digest != menhir_digest:
        raise ReleaseSpecError("Menhir image reference and digest disagree")
    expected_common = {
        "source_commit": menhir_commit,
        "image_tag": expected_tag,
        "image_id": _require_sha256(
            metadata.get("image_id"), "image validation metadata image_id",
            prefixed=True,
        ),
        "config_sha256": _require_sha256(
            metadata.get("config_sha256"),
            "image validation metadata config_sha256",
        ),
        "image_archive_sha256": archive_sha,
    }
    if publication.get("source_repository") != CANONICAL_GITHUB_REPOSITORY:
        raise ReleaseSpecError("image publication source repository is not canonical")
    for document, label in (
        (metadata, "image validation metadata"),
        (publication, "image publication"),
    ):
        for key, expected in expected_common.items():
            if document.get(key) != expected:
                raise ReleaseSpecError(f"{label} {key} is not release-bound")
        if document.get("source_repository") != CANONICAL_GITHUB_REPOSITORY:
            raise ReleaseSpecError(f"{label} source repository is not canonical")
    for key in ("source_commit", "image_tag", "image_id", "config_sha256"):
        if identity.get(key) != expected_common[key]:
            raise ReleaseSpecError(
                f"image validation identity {key} is not release-bound"
            )
    if identity.get("source_repository") != CANONICAL_GITHUB_REPOSITORY:
        raise ReleaseSpecError(
            "image validation identity source repository is not canonical"
        )

    _verify_github_attestation(
        subject=publication_raw,
        bundle=attestation_raw,
        trusted_root=trusted_root_raw,
        repository=CANONICAL_GITHUB_REPOSITORY,
        source_commit=menhir_commit,
    )

    if metadata.get("python_base") != base_ref:
        raise ReleaseSpecError(
            "image validation metadata python_base is not release-bound"
        )
    labels = _exact(
        metadata.get("labels"), IMAGE_LABEL_KEYS, "image validation labels"
    )
    if labels != {
        "commit": menhir_commit,
        "version": version,
        "wheel_manifest_sha256": wheel_manifest_sha256,
        "oauth_wheel_sha256": oauth_wheel_sha256,
    }:
        raise ReleaseSpecError(
            "image validation labels do not bind the exact release inputs"
        )

    if publication.get("registry_digest") != menhir_digest:
        raise ReleaseSpecError("image publication registry digest is not release-bound")
    if publication.get("image_ref") != menhir_ref:
        raise ReleaseSpecError("image publication image_ref is not release-bound")
    if publication.get("candidate_tag") != expected_candidate_tag:
        raise ReleaseSpecError("image publication candidate tag is not archive-bound")
    if publication.get("candidate_ref") != f"{expected_candidate_tag}@{menhir_digest}":
        raise ReleaseSpecError("image publication candidate ref is not digest-bound")
    if not isinstance(publication.get("idempotent_existing_candidate"), bool):
        raise ReleaseSpecError(
            "image publication idempotent_existing_candidate must be boolean"
        )

    metadata_evidence = _exact(
        metadata.get("evidence"), IMAGE_EVIDENCE_KEYS,
        "image validation metadata evidence",
    )
    if metadata_evidence.get("required") is not True:
        raise ReleaseSpecError("image validation evidence must be mandatory")
    identity_evidence = _exact(
        identity.get("evidence"), IMAGE_EVIDENCE_DIGEST_KEYS,
        "image validation identity evidence",
    )
    publication_evidence = _exact(
        publication.get("evidence"), IMAGE_EVIDENCE_DIGEST_KEYS,
        "image publication evidence",
    )
    if publication_evidence != identity_evidence:
        raise ReleaseSpecError(
            "image publication evidence does not bind the validation identity"
        )

    subject = {
        "image_id": expected_common["image_id"],
        "image_archive_sha256": archive_sha,
    }
    evidence_specs = {
        "sbom": (
            sbom_raw,
            "release-image-evidence/sbom.syft.json",
            "syft",
            build_release_image.validate_sbom,
        ),
        "vulnerability_scan": (
            scan_raw,
            "release-image-evidence/scan.grype.json",
            "grype",
            build_release_image.validate_vulnerability_scan,
        ),
    }
    evidence_digests: dict[str, str] = {}
    for kind, (raw, artifact_path, scanner, validator) in evidence_specs.items():
        entry = _exact(
            metadata_evidence.get(kind),
            IMAGE_EVIDENCE_ENTRY_KEYS,
            f"image validation metadata evidence.{kind}",
        )
        digest = _sha256_bytes(raw)
        evidence_digests[kind] = digest
        if entry.get("artifact_path") != artifact_path:
            raise ReleaseSpecError(f"{kind} evidence artifact path is not canonical")
        if entry.get("sha256") != digest or identity_evidence.get(kind) != digest:
            raise ReleaseSpecError(f"{kind} evidence digest is not identity-bound")
        entry_subject = _exact(
            entry.get("subject"), IMAGE_SUBJECT_KEYS,
            f"image validation metadata evidence.{kind}.subject",
        )
        if entry_subject != subject:
            raise ReleaseSpecError(f"{kind} evidence names the wrong release subject")
        try:
            build_release_image._validate_scanner_image(
                entry.get("scanner_image"), scanner
            )
            parsed_validation = validator(
                raw, expected_common["image_id"]
            )
        except build_release_image.BuildImageError as exc:
            raise ReleaseSpecError(f"{kind} evidence is invalid: {exc}") from exc
        if entry.get("validation") != parsed_validation:
            raise ReleaseSpecError(
                f"{kind} evidence validation is not report-bound"
            )
    return {
        "publication_sha256": _sha256_bytes(publication_raw),
        "source_repository": CANONICAL_GITHUB_REPOSITORY,
        "source_commit": menhir_commit,
        "validation_identity_sha256": identity_sha,
        "image_id": expected_common["image_id"],
        "config_sha256": expected_common["config_sha256"],
        "image_archive_sha256": archive_sha,
        "registry_digest": menhir_digest,
        "sbom_sha256": evidence_digests["sbom"],
        "scan_evidence_sha256": evidence_digests["vulnerability_scan"],
        "attestation_bundle_sha256": _sha256_bytes(attestation_raw),
        "attestation_trusted_root_sha256": _sha256_bytes(trusted_root_raw),
    }


def _canonical_json_digest(value: dict[str, Any]) -> str:
    canonical = dict(value)
    canonical.pop("canonical_digest", None)
    return hashlib.sha256(json.dumps(
        canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")).hexdigest()


def _git_run(repo: Path, *args: str, text: bool = True) -> str | bytes:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True, capture_output=True, text=text,
        )
    except subprocess.CalledProcessError as exc:
        raise ReleaseSpecError(
            f"git validation failed for {repo}: {' '.join(args)}"
        ) from exc
    return result.stdout


def _canonical_remote(value: str) -> str:
    match = re.fullmatch(
        r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
        r"([^/]+)/([^/]+?)(?:\.git)?/?",
        value.strip(),
    )
    if not match:
        raise ReleaseSpecError("origin must be a GitHub HTTPS/SSH repository URL")
    return f"https://github.com/{match.group(1)}/{match.group(2)}.git"


def _repo_identity(path_value: Any, name: str) -> tuple[Path, str, str]:
    repo = _directory(path_value, f"repositories.{name}")
    if _git_run(repo, "status", "--porcelain=v1", "--untracked-files=all"):
        raise ReleaseSpecError(f"repository {name} is not clean")
    head = str(_git_run(repo, "rev-parse", "HEAD")).strip()
    if not re.fullmatch(r"[0-9a-f]{40}", head):
        raise ReleaseSpecError(f"repository {name} HEAD is invalid")
    remote = _canonical_remote(
        str(_git_run(repo, "remote", "get-url", "origin")).strip()
    )
    expected = menhir_schema.EXPECTED_REPO_REMOTES[name]
    if remote != expected:
        raise ReleaseSpecError(
            f"repository {name} origin mismatch: expected {expected}, got {remote}"
        )
    tips = set(str(_git_run(
        repo, "for-each-ref", "--format=%(objectname)", "refs/remotes/origin"
    )).splitlines())
    if head not in tips:
        raise ReleaseSpecError(
            f"repository {name} HEAD is not a remote-tracking tip"
        )
    return repo, head, remote


def _git_blob(repo: Path, commit: str, source: str, label: str) -> bytes:
    if not source or source.startswith("/") or "\\" in source or (
        ".." in source.split("/")
    ):
        raise ReleaseSpecError(f"{label} is not a canonical repository path")
    data = _git_run(repo, "show", f"{commit}:{source}", text=False)
    if not isinstance(data, bytes):
        raise AssertionError("binary git output expected")
    return data


def _validate_census(menhir_repo: Path, commit: str) -> None:
    local = _load_json(
        SCRIPT_DIR / "installed-artifacts.json", "installed artifact census"
    )
    _exact(
        local, frozenset({"schema", "destinations"}), "installed artifact census"
    )
    rows = local.get("destinations")
    if local.get("schema") != 1 or not isinstance(rows, list) or not rows or (
        any(not isinstance(row, str) for row in rows)
    ) or len(rows) != len(set(rows)):
        raise ReleaseSpecError("installed artifact census is invalid")
    try:
        committed = json.loads(
            _git_blob(
                menhir_repo, commit, "deploy/installed-artifacts.json", "census"
            ).decode("utf-8"),
            object_pairs_hook=_unique_pairs,
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseSpecError("committed installed artifact census is invalid") from exc
    if committed != local:
        raise ReleaseSpecError(
            "installed artifact census differs from the menhir commit"
        )
    if set(rows) != set(ARTIFACT_SOURCES):
        raise ReleaseSpecError("installed artifact mapping drift")


def _validate_policy(data: bytes) -> str:
    try:
        value = json.loads(
            data.decode("utf-8"), object_pairs_hook=_unique_pairs
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseSpecError("client policy is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict) or value.get("version") != 2:
        raise ReleaseSpecError("client policy must be a version 2 object")
    declared = value.get("canonical_digest")
    if not isinstance(declared, str) or not SHA256_RE.fullmatch(declared):
        raise ReleaseSpecError("client policy canonical_digest is invalid")
    if declared != _canonical_json_digest(value):
        raise ReleaseSpecError("client policy canonical digest mismatch")
    return declared


def _reject_secret_material(value: Any, label: str) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            identifier = key.endswith("_secret_version")
            if SECRET_KEY_RE.search(key) and not identifier and (
                item not in (None, "", False)
            ):
                raise ReleaseSpecError(
                    f"secret-looking config key in {label}: {key}"
                )
            _reject_secret_material(item, f"{label}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_secret_material(item, f"{label}[{index}]")
    elif isinstance(value, str) and SECRET_VALUE_RE.search(value):
        raise ReleaseSpecError(f"secret-looking value in {label}")


def _validate_operations_policy(value: dict[str, Any]) -> None:
    policy = _exact(value, OPERATIONS_POLICY_KEYS, "operations policy")
    if policy.get("schema") != 1:
        raise ReleaseSpecError("operations policy schema must be 1")
    for key in ("issuer", "audience", "base_url"):
        item = policy.get(key)
        if not isinstance(item, str) or not item.startswith("https://"):
            raise ReleaseSpecError(f"operations policy {key} must be an https URL")
    clients = policy.get("clients")
    if not isinstance(clients, dict) or not clients:
        raise ReleaseSpecError("operations policy clients must be non-empty")
    for client_id, raw in clients.items():
        if not isinstance(client_id, str) or not client_id or len(client_id) > 255:
            raise ReleaseSpecError("operations policy client id is malformed")
        client = _exact(
            raw, OPERATIONS_CLIENT_KEYS,
            f"operations policy clients.{client_id}",
        )
        if client.get("tier") not in {"agent", "operator"}:
            raise ReleaseSpecError("operations policy client tier is invalid")
        for field in ("scopes", "tools"):
            rows = client.get(field)
            if not isinstance(rows, list) or not rows or any(
                not isinstance(row, str) or not row or len(row) > 255
                for row in rows
            ) or len(rows) != len(set(rows)):
                raise ReleaseSpecError(
                    f"operations policy client {field} must be unique strings"
                )
        unknown_tools = set(client["tools"]) - OPERATIONS_POLICY_TOOLS
        if unknown_tools:
            raise ReleaseSpecError(
                "operations policy names removed or unknown Menhir tools: "
                + ", ".join(sorted(unknown_tools))
            )
    _reject_secret_material(policy, "operations policy")


def _render_env(path: Path, replacements: dict[str, str]) -> str:
    try:
        lines = path.read_text(encoding="ascii").splitlines()
    except UnicodeError as exc:
        raise ReleaseSpecError("baseline production.env must be ASCII") from exc
    seen: set[str] = set()
    rendered: list[str] = []
    for number, line in enumerate(lines, start=1):
        if not line or line.startswith("#"):
            rendered.append(line)
            continue
        if "=" not in line:
            raise ReleaseSpecError(f"invalid production.env line {number}")
        key, value = line.split("=", 1)
        if not ENV_KEY_RE.fullmatch(key):
            raise ReleaseSpecError(
                f"invalid production.env key on line {number}"
            )
        if key in seen:
            raise ReleaseSpecError(f"duplicate production.env key: {key}")
        seen.add(key)
        if SECRET_KEY_RE.search(key) and value:
            raise ReleaseSpecError(f"secret-looking production.env key: {key}")
        rendered.append(f"{key}={replacements.get(key, value)}")
    missing = sorted(set(replacements) - seen)
    if missing:
        raise ReleaseSpecError(
            f"production.env missing replacement keys: {missing}"
        )
    text = "\n".join(rendered) + "\n"
    if PLACEHOLDER_RE.search(text):
        raise ReleaseSpecError("production.env contains a placeholder")
    if SECRET_VALUE_RE.search(text):
        raise ReleaseSpecError("production.env contains secret-looking material")
    return text


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
        encoding="ascii",
        newline="\n",
    )


def _wheelhouse(path_value: Any) -> tuple[Path, Path, Path, list[dict[str, Any]]]:
    path = _directory(path_value, "evidence.wheelhouse")
    entries = list(path.iterdir())
    if any(item.is_symlink() for item in entries):
        raise ReleaseSpecError("wheelhouse must not contain symlinks")
    wheels = sorted(
        item for item in entries if item.is_file() and item.suffix == ".whl"
    )
    oauth = [item for item in wheels if item.name.startswith("archolith_oauth-")]
    if len(oauth) != 1:
        raise ReleaseSpecError(
            "wheelhouse must contain exactly one archolith_oauth wheel"
        )
    manifest = _regular(path / "SHA256SUMS", "wheelhouse SHA256SUMS")
    try:
        verify_wheelhouse.verify(
            path, manifest, _sha256(manifest), _sha256(oauth[0])
        )
    except ValueError as exc:
        raise ReleaseSpecError(f"wheelhouse validation failed: {exc}") from exc
    records = [
        {
            "filename": item.name,
            "sha256": _sha256(item),
            "size": item.stat().st_size,
        }
        for item in wheels
    ]
    return path, oauth[0], manifest, records


def _validate_output(
    inputs: dict[str, Any], output_path: Path
) -> tuple[Path, Path]:
    workspace = _directory(
        inputs["release_workspace_root"], "release_workspace_root"
    )
    if not output_path.is_absolute():
        raise ReleaseSpecError("output_path must be absolute")
    if output_path.is_symlink():
        raise ReleaseSpecError("output_path must not be a symlink")
    if output_path.name != "release-spec.json" or output_path.parent != workspace:
        raise ReleaseSpecError(
            "output_path must be release_workspace_root/release-spec.json"
        )
    return workspace, workspace / "release-spec-inputs"


def prepare_release_spec(
    inputs_path: Path,
    output_path: Path,
    *,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Validate release inputs and atomically write a release-author spec."""
    raw_inputs = _load_json(inputs_path, "release inputs")
    inputs = _exact(
        raw_inputs,
        INPUT_KEYS_WITH_PROVENANCE
        if isinstance(raw_inputs, dict) and "image_provenance" in raw_inputs
        else INPUT_KEYS,
        "release inputs",
    )
    if inputs.get("schema") != 1:
        raise ReleaseSpecError("release inputs schema must be 1")
    release_id = inputs.get("release_id")
    author = inputs.get("release_author")
    if not isinstance(release_id, str) or not RELEASE_ID_RE.fullmatch(release_id):
        raise ReleaseSpecError("release_id is malformed")
    if not isinstance(author, str) or not AUTHOR_RE.fullmatch(author):
        raise ReleaseSpecError("release_author is malformed")
    workspace, assets_path = _validate_output(inputs, output_path)
    if assets_path.is_symlink():
        raise ReleaseSpecError("release-spec-inputs must not be a symlink")
    if (output_path.exists() or assets_path.exists()) and not overwrite:
        raise ReleaseSpecError("release spec output already exists")

    repo_values = _exact(
        inputs.get("repositories"), REPOSITORIES, "repositories"
    )
    identities = {
        name: _repo_identity(repo_values[name], name)
        for name in sorted(REPOSITORIES)
    }
    repos = {name: identities[name][1] for name in sorted(REPOSITORIES)}
    remotes = {name: identities[name][2] for name in sorted(REPOSITORIES)}
    _validate_census(identities["menhir"][0], identities["menhir"][1])

    ingress_mode = inputs.get("ingress_mode")
    if ingress_mode != "cloudflared":
        raise ReleaseSpecError("ingress_mode must be cloudflared")

    image_values = _exact(inputs.get("images"), IMAGES, "images")
    images: dict[str, str] = {}
    image_refs: dict[str, str] = {}
    for name in sorted(IMAGES):
        row = _exact(image_values[name], IMAGE_KEYS, f"images.{name}")
        digest, reference = row["digest"], row["ref"]
        if not isinstance(digest, str) or not DIGEST_RE.fullmatch(digest):
            raise ReleaseSpecError(f"images.{name}.digest is malformed")
        if not isinstance(reference, str) or not IMAGE_REF_RE.fullmatch(
            reference
        ) or not reference.endswith("@" + digest):
            raise ReleaseSpecError(
                f"images.{name}.ref is not an immutable matching reference"
            )
        images[name] = digest
        image_refs[name] = reference

    image_provenance = inputs.get("image_provenance", "rebuilt")
    if image_provenance not in IMAGE_PROVENANCE_VALUES:
        raise ReleaseSpecError(
            "image_provenance must be 'rebuilt' or 'inherited'"
        )
    inherited_image = image_provenance == "inherited"
    evidence_values = _exact(
        inputs.get("evidence"),
        INHERITED_EVIDENCE_KEYS if inherited_image else EVIDENCE_KEYS,
        "evidence",
    )
    wheelhouse, oauth_wheel, docker_manifest, wheel_records = _wheelhouse(
        evidence_values["wheelhouse"]
    )
    sbom = _regular(evidence_values["sbom"], "evidence.sbom")
    scan = _regular(evidence_values["scan"], "evidence.scan")
    if inherited_image:
        image_publication = None
        image_metadata = None
        image_identity = None
        image_archive = None
        publication_attestation = None
        attestation_trusted_root = None
    else:
        image_publication = _regular(
            evidence_values["image_publication"], "evidence.image_publication"
        )
        image_metadata = _regular(
            evidence_values["image_metadata"], "evidence.image_metadata"
        )
        image_identity = _regular(
            evidence_values["image_identity"], "evidence.image_identity"
        )
        image_archive = _regular(
            evidence_values["image_archive"], "evidence.image_archive"
        )
        publication_attestation = _regular(
            evidence_values["publication_attestation"],
            "evidence.publication_attestation",
        )
        attestation_trusted_root = _regular(
            evidence_values["attestation_trusted_root"],
            "evidence.attestation_trusted_root",
        )
    baseline = _regular(
        inputs["baseline_production_env"], "baseline_production_env"
    )
    operations_path = _regular(inputs["operations_policy"], "operations_policy")
    public_key = _regular(inputs["oauth_public_key"], "oauth_public_key")
    runtime_digest = _regular(
        inputs["python_runtime_digest"], "python_runtime_digest"
    )
    prior_release = _regular(inputs["prior_release"], "prior_release")
    prior_route = _regular(inputs["prior_route"], "prior_route")
    prior = menhir_schema.validate_release(str(prior_release))
    if prior.get("release_id") == release_id:
        raise ReleaseSpecError("release_id must differ from prior release")
    if inherited_image:
        # The whole basis of an inherited release: these must be the same bytes
        # the prior authority already bound. Any difference means something was
        # built, and a built image must carry its own attestation.
        prior_images = prior.get("images")
        if not isinstance(prior_images, dict):
            raise ReleaseSpecError(
                "prior release does not record images; cannot inherit"
            )
        for name in sorted(IMAGES):
            declared = images.get(name)
            inherited = prior_images.get(name)
            if declared != inherited:
                raise ReleaseSpecError(
                    "inherited image %s does not match the prior release: "
                    "%s != %s" % (name, declared, inherited)
                )
    yawn_env_sha256 = inputs.get("yawn_env_sha256")
    if not isinstance(yawn_env_sha256, str) or not DIGEST_RE.fullmatch(
        yawn_env_sha256
    ):
        raise ReleaseSpecError("yawn_env_sha256 must be a sha256 digest")

    runtime_bytes = runtime_digest.read_bytes()
    public_key_bytes = public_key.read_bytes()
    operations_bytes = operations_path.read_bytes()
    try:
        runtime_text = runtime_bytes.decode("ascii").strip()
        public_key_text = public_key_bytes.decode("ascii")
    except UnicodeError as exc:
        raise ReleaseSpecError("runtime digest and public key must be ASCII") from exc
    if not SHA256_HEX_RE.fullmatch(runtime_text):
        raise ReleaseSpecError(
            "python runtime digest file must contain one sha256 digest"
        )
    if "-----BEGIN PUBLIC KEY-----" not in public_key_text or (
        "PRIVATE KEY" in public_key_text
    ) or PLACEHOLDER_RE.search(public_key_text):
        raise ReleaseSpecError(
            "OAuth public key must contain a concrete public PEM key"
        )

    menhir_repo, menhir_commit, _ = identities["menhir"]
    policy_bytes = _git_blob(
        menhir_repo,
        menhir_commit,
        "deploy/client-policy.production.json",
        "client policy",
    )
    policy_digest = _validate_policy(policy_bytes)
    secret_versions = _exact(
        inputs.get("secret_version_ids"),
        SECRET_VERSIONS,
        "secret_version_ids",
    )
    for name, value in secret_versions.items():
        if not isinstance(value, str) or not VERSION_RE.fullmatch(value):
            raise ReleaseSpecError(f"secret_version_ids.{name} is malformed")
    expected_policy_version = "sha256-" + policy_digest
    if secret_versions["client-policy"] != expected_policy_version:
        raise ReleaseSpecError(
            "secret_version_ids.client-policy must bind the client policy digest"
        )

    if not inherited_image:
        # A rebuilt image must revalidate its full publication chain here.
        # An inherited image has no new chain; its digests were proven equal
        # to the prior release authority above.
        validate_image_publication(
            publication_path=image_publication,
            metadata_path=image_metadata,
            identity_path=image_identity,
            archive_path=image_archive,
            sbom_path=sbom,
            scan_path=scan,
            publication_attestation_path=publication_attestation,
            attestation_trusted_root_path=attestation_trusted_root,
            menhir_commit=menhir_commit,
            menhir_digest=images["menhir"],
            menhir_ref=image_refs["menhir"],
            base_ref=image_refs["base"],
            release_id=release_id,
            wheel_manifest_sha256=_sha256(docker_manifest),
            oauth_wheel_sha256=_sha256(oauth_wheel),
        )

    operations = _load_json_bytes(operations_bytes, "operations policy")
    _validate_operations_policy(operations)

    production_env = _render_env(baseline, {
        "MENHIR_IMAGE": image_refs["menhir"],
        "NEO4J_IMAGE": image_refs["neo4j"],
        "MENHIR_RELEASE_COMMIT": menhir_commit,
        "MENHIR_RELEASE_ID": release_id,
        "MENHIR_CLIENT_POLICY_DIGEST": policy_digest,
    })

    stage = Path(tempfile.mkdtemp(prefix=".release-spec.", dir=workspace))
    published_assets = False
    try:
        staged_assets = stage / "release-spec-inputs"
        staged_assets.mkdir()
        generated = {
            "menhir_compose_sha256":
                staged_assets / "docker-compose.production.yml",
            "yawn_compose_sha256": staged_assets / "yawn-docker-compose.yml",
            "caddy_sha256": staged_assets / "Caddyfile",
            "registry_sha256": staged_assets / "releases.json",
            "policy_sha256": staged_assets / "client-policy.json",
            "production_env_sha256": staged_assets / "production.env",
            "operations_policy_sha256":
                staged_assets / "menhir-oauth-policy.json",
            "oauth_public_key_sha256":
                staged_assets / "menhir-oauth-public.pem",
            "python_runtime_digest_sha256":
                staged_assets / "menhir-python-runtime.sha256",
        }
        blobs = {
            "menhir_compose_sha256":
                ("menhir", "deploy/docker-compose.production.yml"),
            "yawn_compose_sha256": ("yawn_deploy", "docker-compose.yml"),
            "caddy_sha256": ("yawn_deploy", "Caddyfile"),
            "registry_sha256": ("yawn_deploy", "releases.json"),
        }
        for key, (repo_name, source) in blobs.items():
            repo, commit, _ = identities[repo_name]
            generated[key].write_bytes(_git_blob(repo, commit, source, key))
        generated["policy_sha256"].write_bytes(policy_bytes)
        generated["production_env_sha256"].write_text(
            production_env, encoding="ascii", newline="\n"
        )
        generated["operations_policy_sha256"].write_bytes(
            operations_bytes
        )
        generated["oauth_public_key_sha256"].write_bytes(
            public_key_bytes
        )
        generated["python_runtime_digest_sha256"].write_bytes(
            runtime_bytes
        )
        for key, path in generated.items():
            if key == "oauth_public_key_sha256":
                continue
            try:
                content = path.read_text(encoding="utf-8")
            except UnicodeError as exc:
                raise ReleaseSpecError(
                    f"generated {key} is not valid UTF-8"
                ) from exc
            if SECRET_VALUE_RE.search(content):
                raise ReleaseSpecError(
                    f"generated {key} contains secret-looking material"
                )

        def final(name: str) -> str:
            return str((assets_path / name).resolve())

        wheel_manifest_path = staged_assets / "wheel-build.json"
        _write_json(wheel_manifest_path, {
            "schema": 1,
            "kind": "menhir-wheel-build-evidence",
            "source_repository": remotes["archolith_oauth"],
            "source_commit": repos["archolith_oauth"],
            "image_refs": image_refs,
            "wheels": wheel_records,
        })
        provenance_path = staged_assets / "provenance.json"
        _write_json(provenance_path, {
            "schema": 1,
            "repos": repos,
            "repo_remotes": remotes,
            "images": images,
            "oauth_wheel_sha256": _sha256(oauth_wheel),
            "wheel_manifest_sha256": _sha256(wheel_manifest_path),
            "dockerfile_wheel_manifest_sha256": _sha256(docker_manifest),
        })
        rendered = {
            key: final(path.name) for key, path in generated.items()
        }
        rendered["yawn_env_sha256"] = yawn_env_sha256
        spec = {
            "schema": 1,
            "release_id": release_id,
            "release_author": author,
            "ingress_mode": ingress_mode,
            "repositories": {
                name: str(identities[name][0]) for name in sorted(REPOSITORIES)
            },
            "images": images,
            "evidence": {
                "oauth_wheel": str(oauth_wheel),
                "wheelhouse": str(wheelhouse),
                "wheel_manifest": final("wheel-build.json"),
                "dockerfile_wheel_manifest": str(docker_manifest),
                "sbom": str(sbom),
                "scan": str(scan),
                "provenance": final("provenance.json"),
            },
            "rendered": rendered,
            "network": {
                "project": "menhir-prod",
                "external_network": "menhir-proxy",
                "alias": "menhir-prod-app",
                "peers": ["172.30.0.2"],
            },
            "initial_release": False,
            "prior_release": str(prior_release),
            "prior_route": str(prior_route),
            "initial_prior_images": None,
            "secret_version_ids": secret_versions,
            "artifact_sources": ARTIFACT_SOURCES,
            "initial_host_state": None,
            "image_provenance": image_provenance,
        }
        if not inherited_image:
            # A rebuilt release carries the full publication and attestation
            # chain for the bytes it produced. Unchanged from before this
            # branch existed.
            spec["image_refs"] = image_refs
            spec["evidence"].update({
                "image_publication": str(image_publication),
                "image_metadata": str(image_metadata),
                "image_identity": str(image_identity),
                "image_archive": str(image_archive),
                "publication_attestation": str(publication_attestation),
                "attestation_trusted_root": str(attestation_trusted_root),
            })
        else:
            # Name the authority the image binding is inherited from, so the
            # chain stays followable: these bytes are attested exactly as
            # strongly as that release attested them, and no more.
            spec["inherited_image_release"] = {
                "release_id": prior.get("release_id"),
                "release_sha256": _sha256(prior_release),
            }
        staged_spec = stage / "release-spec.json"
        _write_json(staged_spec, spec)
        if PLACEHOLDER_RE.search(staged_spec.read_text(encoding="ascii")):
            raise ReleaseSpecError(
                "generated release spec contains a placeholder"
            )

        if overwrite and assets_path.exists():
            if assets_path.is_symlink() or not assets_path.is_dir():
                raise ReleaseSpecError("existing release-spec-inputs is unsafe")
            shutil.rmtree(assets_path)
        os.replace(staged_assets, assets_path)
        published_assets = True
        if output_path.exists() and not overwrite:
            raise ReleaseSpecError("release spec output already exists")
        os.replace(staged_spec, output_path)
        published_assets = False
        return spec
    except Exception:
        if published_assets and assets_path.exists() and not output_path.exists():
            shutil.rmtree(assets_path)
        raise
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv[1:])
    try:
        prepare_release_spec(
            args.inputs, args.output, overwrite=args.overwrite
        )
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"release spec preparation failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
