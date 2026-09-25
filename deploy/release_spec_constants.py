"""Static vocabularies and compiled patterns for release specification inputs."""

from __future__ import annotations

import re

REPOSITORIES = frozenset({"menhir", "archolith_oauth", "yawn_deploy"})
IMAGES = frozenset({"menhir", "neo4j", "caddy", "base"})
SECRET_VERSIONS = frozenset({
    "neo4j-auth", "neo4j-password", "oauth-signing-key",
    "oauth-retry-keyring", "oauth-consent-secret", "operator-key",
    "client-policy", "provider-key",
})
INPUT_KEYS = frozenset({
    "schema", "release_id", "release_author", "release_workspace_root",
    "repositories", "images", "evidence", "baseline_production_env",
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
