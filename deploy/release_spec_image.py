"""GitHub publication attestation and image evidence chain validation."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import Any

import build_release_image

from release_spec_constants import DIGEST_RE, SHA256_RE
from release_spec_errors import ReleaseSpecError
from release_spec_io import _capture_bytes, _exact, _load_json_bytes, _sha256, _sha256_bytes

CANONICAL_GITHUB_REPOSITORY = "Archolith/menhir"
REVIEWED_GITHUB_ATTESTATION_TRUSTED_ROOT_SHA256 = (
    "65ca537f6ed8a47fd0e560c421baa1f6c1efb8b25fc200d8c5c02c0e92eb2b9c"
)
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
