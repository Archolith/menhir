"""Shared install-bundle constants and the duplicate-key JSON error."""

from __future__ import annotations

import re

SPEC_KEYS = frozenset({
    "schema", "release_id", "release_author", "repositories", "images",
    "image_refs",
    "evidence", "rendered", "network", "initial_release", "prior_release",
    "prior_route", "initial_prior_images", "secret_version_ids",
    "artifact_sources", "initial_host_state", "deployment_class",
    "notes_json_sha256", "notes_markdown_sha256", "ingress_mode",
})
# See release_spec.INHERITED_EVIDENCE_KEYS. An inherited-image spec carries no
# image_refs and no publication evidence, and names the authority it inherits from.
SPEC_KEYS_WITH_PROVENANCE = SPEC_KEYS | frozenset({"image_provenance"})
SPEC_KEYS_INHERITED = (
    (SPEC_KEYS - frozenset({"image_refs"}))
    | frozenset({"image_provenance", "inherited_image_release"})
)
REPOSITORIES = frozenset({"menhir", "archolith_oauth", "yawn_deploy"})
EVIDENCE_DIGESTS = {
    "oauth_wheel": "oauth_wheel_sha256",
    "wheel_manifest": "wheel_manifest_sha256",
    "dockerfile_wheel_manifest": "dockerfile_wheel_manifest_sha256",
    "sbom": "sbom_sha256",
    "scan": "scan_evidence_sha256",
    "provenance": "provenance_sha256",
}
PUBLICATION_EVIDENCE = frozenset({
    "image_publication", "image_metadata", "image_identity", "image_archive",
    "publication_attestation", "attestation_trusted_root",
})
RENDERED_DESTINATIONS = {
    "/srv/menhir/production/release/production.env": "production_env_sha256",
}
RELEASE_DESTINATION = "/srv/menhir/production/release/release.json"
STAGING_RUNNER_DESTINATION = "/srv/menhir/scaffold/bin/menhir_stage_vps.py"
MANIFEST_NAME = "bundle-manifest.json"
INSTALLER_NAME = "install.sh"
INSTALLER_SOURCE_NAME = "release-install.sh"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
OID_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
ALLOWED_GIT_MODES = frozenset({"100644", "100755"})


class DuplicateKeyError(ValueError):
    """Raised when a JSON object repeats a key."""
