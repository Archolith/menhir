"""Shared release-flow constants and the release-flow error type."""

from __future__ import annotations

import re


STATE_NAME = "release-flow.json"
SPEC_NAME = "release-spec.json"
NOTES_JSON_NAME = "release-notes.json"
NOTES_MARKDOWN_NAME = "release-notes.md"
REVIEW_REQUEST_NAME = "security-review-request.json"
RELEASE_NAME = "release.json"
BUNDLE_NAME = "install-bundle"
PUBLICATION_RECEIPT_NAME = "publication-receipt.json"
KIND = "menhir-release-flow"
PUBLICATION_KIND = "menhir-release-publication"
SCHEMA = 1
PHASES = ("review_requested", "bundled", "publishing", "published", "deployed")
REPOSITORIES = frozenset({"menhir", "archolith_oauth", "yawn_deploy"})
CLASS_ORDER = {"app-only": 0, "security-config": 1, "maintenance": 2}
SECURITY_CONFIG_PATHS = tuple(re.compile(pattern) for pattern in (
    r"^src/menhir/config/",
    r"^src/menhir/api/client_policy\.py$",
))
APP_ONLY_SOURCE_PATHS = tuple(re.compile(pattern) for pattern in (
    r"^src/menhir/explorer/static/[^/]+$",
    r"^src/menhir/explorer/templates/[^/]+$",
))
MAINTENANCE_PATHS = tuple(re.compile(pattern) for pattern in (
    r"^deploy/",
    r"^\.github/",
    r"^(pyproject\.toml|uv\.lock|poetry\.lock|requirements[^/]*)$",
    r"^src/menhir/core/(bootstrap|runtime|runtime_preflight)\.py$",
    r"^src/menhir/infrastructure/(schema|migration_batches|embedding_dimensions)\.py$",
    r"^src/menhir/infrastructure/telemetry/schema_migrations\.py$",
))
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
RELEASE_ID_RE = re.compile(r"^menhir-prod-[0-9]+\.[0-9]+\.[0-9]+-[0-9]+$")
RELEASE_ID_PARTS_RE = re.compile(
    r"^menhir-prod-(?P<version>[0-9]+\.[0-9]+\.[0-9]+)-(?P<sequence>[0-9]+)$"
)
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
PUBLICATION_NONCE_RE = re.compile(r"^[0-9a-f]{32}$")
LEGACY_STATE_KEYS = frozenset({
    "schema", "kind", "phase", "release_id", "release_author", "workspace",
    "deployment_class", "inputs_sha256", "spec_sha256", "notes_json_sha256",
    "notes_markdown_sha256", "review_request_sha256", "security_review_sha256",
    "release_sha256", "bundle_manifest_sha256", "bundle_sha256",
})
PRE_INGRESS_STATE_KEYS = LEGACY_STATE_KEYS | frozenset({
    "fragments_dir", "fragments", "publication_nonce",
    "publication_receipt_sha256",
})
STATE_KEYS = PRE_INGRESS_STATE_KEYS | frozenset({"ingress_mode"})
FROZEN_ARTIFACT_KEYS = (
    "spec_sha256",
    "notes_json_sha256",
    "notes_markdown_sha256",
    "review_request_sha256",
    "security_review_sha256",
    "release_sha256",
    "bundle_manifest_sha256",
    "bundle_sha256",
)
RELEASE_AUTHORITY_BINDINGS = (
    "deployment_class",
    "ingress_mode",
    "notes_json_sha256",
    "notes_markdown_sha256",
)


class ReleaseFlowError(ValueError):
    """A release-flow input, state, or transition is invalid."""
