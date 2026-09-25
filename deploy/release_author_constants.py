"""Static vocabularies, patterns, and the installed-artifact census for release authoring."""

from __future__ import annotations

import json
import re
from pathlib import Path

import menhir_schema

SCRIPT_DIR = Path(__file__).resolve().parent


SPEC_KEYS = frozenset({
    "schema", "release_id", "release_author", "repositories", "images",
    "image_refs", "evidence",
    "rendered", "network", "initial_release", "prior_release",
    "prior_route", "initial_prior_images", "secret_version_ids",
    "artifact_sources",
    "initial_host_state", "deployment_class", "notes_json_sha256",
    "notes_markdown_sha256", "ingress_mode",
})
SECURITY_REVIEW_KEYS = frozenset({
    "schema", "kind", "review_id", "release_author", "reviewer",
    "reviewed_utc", "authority_sha256", "verdict", "unresolved_findings",
    "scope", "report_sha256",
})
REPOSITORIES = frozenset({"menhir", "archolith_oauth", "yawn_deploy"})
IMAGES = frozenset({"menhir", "neo4j", "caddy", "base"})
EVIDENCE = frozenset({
    "oauth_wheel", "wheelhouse", "wheel_manifest",
    "dockerfile_wheel_manifest", "sbom", "scan", "image_publication",
    "image_metadata", "image_identity", "image_archive", "provenance",
    "publication_attestation", "attestation_trusted_root",
})
# A spec may declare image_provenance. "inherited" ships the prior release's
# image unchanged: it carries no image_refs and no image attestation evidence,
# and instead names the authority it inherits the binding from. See
# release_spec.INHERITED_EVIDENCE_KEYS for the rationale and its limits.
SPEC_KEYS_WITH_PROVENANCE = SPEC_KEYS | frozenset({"image_provenance"})
SPEC_KEYS_INHERITED = (
    (SPEC_KEYS - frozenset({"image_refs"}))
    | frozenset({"image_provenance", "inherited_image_release"})
)
INHERITED_EVIDENCE = EVIDENCE - frozenset({
    "image_publication", "image_metadata", "image_identity", "image_archive",
    "publication_attestation", "attestation_trusted_root",
})
RENDERED = frozenset({
    "menhir_compose_sha256", "yawn_compose_sha256", "caddy_sha256",
    "registry_sha256", "policy_sha256", "yawn_env_sha256",
    "production_env_sha256",
})
LITERAL_RENDERED_DIGESTS = frozenset({"yawn_env_sha256"})
SECRET_VERSIONS = frozenset({
    "neo4j-auth", "neo4j-password", "oauth-signing-key",
    "oauth-retry-keyring", "oauth-consent-secret", "operator-key",
    "client-policy", "provider-key",
})
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
DEPLOYMENT_CLASSES = frozenset({"app-only", "security-config", "maintenance"})
INGRESS_MODES = frozenset({"cloudflared"})
ALLOWED_ARTIFACT_PREFIXES = ("/srv/menhir/production/", "/etc/sudoers.d/",
                             "/etc/systemd/system/", "/etc/tmpfiles.d/",
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

