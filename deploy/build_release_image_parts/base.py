"""Shared constants, the release-image error type, and SHA-256 helpers."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

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
CANONICAL_SOURCE_REPOSITORY = "Archolith/menhir"


class BuildImageError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()
