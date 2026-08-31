"""Strict, duplicate-key-rejecting schemas for Menhir production release artifacts.

This module is the single source of truth for the shape of the immutable
deployment-authority records Menhir owns:

  * ``MANIFEST.json``   - one backup generation: exact set equality against the
                          generation directory, per-file classification
                          (authority/secret/config/disposable), and binding to
                          ``SHA256SUMS``. Extras and unclassified files are
                          rejected.
  * ``release.json``    - immutable root-owned release authority (blocker 7).
                          Unknown labels are rejected; image pins must be
                          digest-pinned; the Dockerfile wheel-hash manifest is
                          mandatory.
  * ``receipt``         - atomic structured receipts emitted by the backup
                          local backup wrapper, the restore rehearsal, and the
                          candidate-acceptance verifier. Promotion consumes the
                          exact parsed fields and never reads mtime.

Every loader rejects duplicate object keys so a JSON document with two
definitions of the same key can never be accepted as two different things.

This module has no runtime dependencies beyond the standard library, so it is
unit-testable without Docker.
"""

from __future__ import annotations

import hashlib
import json
import ntpath
import os
import posixpath
import re
import stat
import sys
from datetime import datetime, timedelta, timezone

SCHEMA_VERSION = 1

_GENERATION_RE = re.compile(r"^generation\.[A-Za-z0-9]+$")
_OPERATION_JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")

# Per-file durability classification. Every file in a generation must carry
# exactly one of these; anything unclassified is rejected.
FILE_CLASSES = frozenset({"authority", "secret", "config", "disposable"})

# Marker files that are part of a generation but are NOT themselves enumerated
# in the manifest `files` map (they bind the manifest rather than the data).
_GEN_MARKERS = frozenset({"MANIFEST.json", "SHA256SUMS", "COMPLETE"})

_MANIFEST_TOP_KEYS = frozenset(
    {"schema", "generation", "created_utc", "build", "release", "restore_order",
     "files", "sha256sums_sha256"}
)
_BUILD_KEYS = frozenset(
    {"repo_commit", "menhir_image", "menhir_image_digest",
     "neo4j_image", "neo4j_image_digest"}
)
_RELEASE_KEYS = frozenset({"release_id", "release_manifest_sha256"})

# Required authority files that every complete generation must enumerate.
_REQUIRED_AUTHORITY = frozenset({
    "neo4j/neo4j.dump",
    "neo4j/system.dump",
    "state/oauth/menhir_oauth_as.db",
    "state/telemetry/mcp_telemetry.db",
    "secrets/neo4j/neo4j-auth",
    "secrets/menhir/neo4j-password",
    "secrets/menhir/operator-key",
    "secrets/oauth/oauth_signing_key.json",
    "secrets/oauth/retry-response-keyring.json",
    "secrets/oauth/oauth-consent-secret",
    "policy/client-policy.json",
    "config/docker-compose.production.yml",
    "config/Dockerfile",
    "config/production.env",
    "config/release.json",
    "config/durable-state-inventory.json",
    "config/commit.txt",
})

_RELEASE_TOP_KEYS = frozenset({
    "schema", "release_id", "release_author", "security_review", "repos",
    "oauth_wheel_sha256", "oauth_wheel_source", "images",
    "wheel_manifest_sha256", "dockerfile_wheel_manifest_sha256",
    "sbom_sha256", "scan_evidence_sha256", "provenance_sha256",
    "rendered", "network", "rollback_anchors", "secret_version_ids",
    "artifacts", "repo_remotes", "deployment",
})
_RELEASE_SECURITY_REVIEW_KEYS = frozenset({
    "schema", "kind", "review_id", "release_author", "reviewer",
    "reviewed_utc", "authority_sha256", "verdict", "unresolved_findings",
    "scope", "report_sha256", "review_artifact_sha256",
})
_RELEASE_SECURITY_FINDINGS = frozenset({"critical", "high"})
REQUIRED_SECURITY_REVIEW_SCOPE = frozenset({
    "authentication-and-oauth-authority",
    "authorization-and-client-tool-policy",
    "secret-handling",
    "network-and-ingress-boundaries",
    "host-privilege-and-command-wrappers",
    "supply-chain-and-build-evidence",
    "backup-restore-and-rollback",
    "runtime-hardening-and-observability",
})
_RELEASE_REPOS = frozenset({"menhir", "archolith_oauth", "yawn_deploy", "yawn_vps"})
_RELEASE_IMAGES = frozenset({"menhir", "neo4j", "caddy", "base"})
_RELEASE_RENDERED = frozenset({
    "menhir_compose_sha256", "yawn_compose_sha256", "caddy_sha256",
    "registry_sha256", "policy_sha256", "yawn_env_sha256",
    "production_env_sha256", "operations_policy_sha256",
    "oauth_public_key_sha256", "python_runtime_digest_sha256",
})
_RELEASE_NETWORK = frozenset({
    "project", "external_network", "alias", "peers",
})
_RELEASE_DEPLOYMENT = frozenset({
    "topology", "legacy_container", "production_container",
    "candidate_container", "legacy_database_container",
    "candidate_database_container", "compose_project", "compose_service",
})
_EXPECTED_DEPLOYMENT = {
    "topology": "same-host-docker",
    "legacy_container": "menhir-prod-app",
    "production_container": "menhir-prod-app",
    "candidate_container": "menhir-candidate-app",
    "legacy_database_container": "menhir-prod-neo4j",
    "candidate_database_container": "menhir-candidate-neo4j",
    "compose_project": "menhir-prod",
    "compose_service": "menhir",
}
_RELEASE_ROLLBACK = frozenset({
    "initial_release", "prior_release_id", "prior_release_sha256",
    "prior_images", "prior_route_sha256", "initial_host_state_sha256",
})
_RELEASE_PRIOR_IMAGES = frozenset({"menhir", "neo4j", "caddy"})
_RELEASE_OAUTH_WHEEL_SOURCE = frozenset({
    "repository", "commit", "source_tree_sha256", "wheel_sha256",
})
_RELEASE_SECRET_VERSIONS = frozenset({
    "neo4j-auth", "neo4j-password", "oauth-signing-key",
    "oauth-retry-keyring", "oauth-consent-secret", "operator-key",
    "client-policy", "provider-key",
})
_RELEASE_GIT_ARTIFACT_ENTRY = frozenset({
    "kind", "sha256", "repository", "commit", "path", "blob_oid",
})
_RELEASE_RENDERED_ARTIFACT_ENTRY = frozenset({
    "kind", "sha256", "rendered_key",
})
_RELEASE_REQUIRED_RENDERED_ARTIFACTS = {
    "/srv/menhir/production/release/production.env": "production_env_sha256",
    "/etc/yawn-vps/menhir-oauth-policy.json": "operations_policy_sha256",
    "/etc/yawn-vps/menhir-oauth-public.pem": "oauth_public_key_sha256",
    "/etc/yawn-vps/menhir-python-runtime.sha256": "python_runtime_digest_sha256",
}

# Safe, monotonic release identity shape. Every release_id MUST match so a
# caller-supplied label cannot smuggle path/traversal/metacharacter content
# into the immutable authority record and so rollback ordering is unambiguous.
_RELEASE_ID_RE = re.compile(r"^menhir-prod-[0-9]+\.[0-9]+\.[0-9]+-[0-9]+$")

# Expected git remote origin identities for the four release repositories.
# The release author verifies each checked-out repository's remote origin
# against its expected identity so a rebuild from a fork/mirror (or a
# URL-injected substitute) is refused rather than silently recorded.
# Each pattern is matched (re.search) against `git remote get-url origin`.
EXPECTED_REPO_REMOTES = {
    "menhir": "https://github.com/Archolith/menhir.git",
    "archolith_oauth": "https://github.com/Archolith/archolith_oauth.git",
    "yawn_deploy": "https://github.com/ctharvey/yawn.deploy.git",
    "yawn_vps": "https://github.com/ctharvey/yawn.vps.git",
}

# Source and external evidence use Ed25519. Only raw public keys are release
# inputs; source/worker private keys never exist on the target VPS.
_B64URL_RE = re.compile(r"^[A-Za-z0-9_-]+$")

_PREREQUISITE_KEYS = frozenset({
    "schema", "kind", "release_id", "release_manifest_sha256", "checked_utc",
    "route_version", "observations",
})
_PREREQUISITE_CHECKS = frozenset({
    "firewall", "proxied_dns", "full_strict", "hostname_aop",
    "external_scan", "console_recovery", "caddy_volume_permissions",
})
_PREREQUISITE_OBSERVATION_KEYS = frozenset({
    "worker_id", "network_id", "observed_utc", "route_version", "checks", "signature",
})

_SOURCE_FENCE_KEYS = frozenset({
    "schema", "kind", "release_id", "release_manifest_sha256", "checked_utc",
    "expires_utc", "source_id", "source_writer_stopped",
    "source_mutation_probe_denied", "source_service_disabled",
    "source_firewall_persistent", "signing_key_id", "signature",
})

# Canonical source-fence claims that are covered by the HMAC-SHA256 signature.
# The signature binds release identity + the fenced source identity + the
# explicit window so a tampered or replayed fence cannot be substituted.
_SOURCE_FENCE_SIGNED = frozenset({
    "release_id", "release_manifest_sha256", "checked_utc", "expires_utc",
    "source_id", "source_writer_stopped", "source_mutation_probe_denied",
    "source_service_disabled", "source_firewall_persistent", "signing_key_id",
})


def _reject_duplicates(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate object key: %r" % key)
        out[key] = value
    return out


def load_strict(path: str) -> dict:
    """Load a JSON document, rejecting duplicate object keys."""
    with open(path, encoding="utf-8") as handle:
        return json.load(handle, object_pairs_hook=_reject_duplicates)


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_str(value, label):
    if not isinstance(value, str) or not value:
        raise ValueError("%s must be a non-empty string" % label)
    return value


def _parse_utc(value, label):
    value = _require_str(value, label)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("%s must be an ISO-8601 timestamp" % label) from exc
    if parsed.tzinfo is None:
        raise ValueError("%s must include a timezone" % label)
    return parsed.astimezone(timezone.utc)


def _require_fresh(value, label, max_age_seconds):
    parsed = _parse_utc(value, label)
    now = datetime.now(timezone.utc)
    if parsed > now + timedelta(seconds=60):
        raise ValueError("%s is in the future" % label)
    if parsed < now - timedelta(seconds=max_age_seconds):
        raise ValueError("%s is stale" % label)
    return parsed


def _require_sha256(value, label):
    value = _require_str(value, label)
    if not _SHA256_RE.match(value):
        raise ValueError("%s must be a 64-char lowercase sha256" % label)
    return value


def release_authority_sha256(release: dict) -> str:
    """Digest every release claim except the security review itself."""
    if not isinstance(release, dict):
        raise ValueError("release authority must be a JSON object")
    authority = dict(release)
    authority.pop("security_review", None)
    payload = json.dumps(
        authority, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def _require_digest(value, label):
    value = _require_str(value, label)
    if not _DIGEST_RE.match(value):
        raise ValueError("%s must be digest-pinned (sha256:<64hex>)" % label)
    return value


def _require_release_id(value, label):
    value = _require_str(value, label)
    if not _RELEASE_ID_RE.match(value):
        raise ValueError(
            "%s violates the safe release_id contract "
            "(menhir-prod-<major>.<minor>.<patch>-<seq>)" % label
        )
    return value


def _require_key_id(value, label):
    value = _require_str(value, label)
    if len(value) > 64 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value):
        raise ValueError("%s must be a safe bounded key id" % label)
    return value


def _require_exact_keys(mapping: dict, allowed: frozenset, label: str) -> None:
    if not isinstance(mapping, dict):
        raise ValueError("%s must be a JSON object" % label)
    actual = set(mapping)
    extra = actual - allowed
    missing = allowed - actual
    if extra or missing:
        details = []
        if missing:
            details.append("missing label(s): %s" % ", ".join(sorted(missing)))
        if extra:
            details.append("unknown label(s): %s" % ", ".join(sorted(extra)))
        raise ValueError("%s has invalid labels; %s" % (label, "; ".join(details)))


def _reject_symlinks_and_special(root: str) -> None:
    """Refuse symlinks and special entries anywhere inside the tree."""
    for dirpath, dirnames, filenames in os.walk(root):
        for name in sorted(dirnames) + sorted(filenames):
            full = os.path.join(dirpath, name)
            if os.path.islink(full) or not (os.path.isfile(full) or os.path.isdir(full)):
                raise ValueError(
                    "generation contains symlink or special entry: %s"
                    % os.path.relpath(full, root).replace(os.sep, "/"))


def _walk_regular_files(root: str):
    _reject_symlinks_and_special(root)
    result = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            if os.path.isfile(full) and not os.path.islink(full):
                rel = os.path.relpath(full, root).replace(os.sep, "/")
                result.append(rel)
    return result


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

def validate_manifest(manifest_path: str, root: str) -> dict:
    """Validate a generation MANIFEST.json against the generation directory."""
    manifest = load_strict(manifest_path)
    if not isinstance(manifest, dict):
        raise ValueError("manifest must be a JSON object")
    _require_exact_keys(manifest, _MANIFEST_TOP_KEYS, "manifest")
    if manifest.get("schema") != SCHEMA_VERSION:
        raise ValueError("manifest schema must be %d" % SCHEMA_VERSION)

    generation = _require_str(manifest.get("generation"), "manifest.generation")
    if not _GENERATION_RE.match(generation):
        raise ValueError("manifest.generation is invalid: %r" % generation)
    _parse_utc(manifest.get("created_utc"), "manifest.created_utc")

    build = manifest.get("build")
    _require_exact_keys(build, _BUILD_KEYS, "manifest.build")
    if not _COMMIT_RE.match(_require_str(build.get("repo_commit"), "build.repo_commit")):
        raise ValueError("build.repo_commit must be a 40-char lowercase hex commit")
    _require_digest(build.get("menhir_image_digest"), "build.menhir_image_digest")
    _require_digest(build.get("neo4j_image_digest"), "build.neo4j_image_digest")

    release = manifest.get("release")
    if release is not None:
        _require_exact_keys(release, _RELEASE_KEYS, "manifest.release")
        _require_str(release.get("release_id"), "release.release_id")
        _require_sha256(release.get("release_manifest_sha256"),
                        "release.release_manifest_sha256")

    restore_order = manifest.get("restore_order")
    if not isinstance(restore_order, list) or not restore_order or \
            any(not isinstance(x, str) or not x for x in restore_order):
        raise ValueError("manifest.restore_order must be a non-empty list of strings")

    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise ValueError("manifest.files must be a non-empty object")

    # Exact set equality against the generation directory contents.
    declared = set(files)
    actual = set(p for p in _walk_regular_files(root) if p not in _GEN_MARKERS)
    if declared != actual:
        extras = sorted(actual - declared)
        missing = sorted(declared - actual)
        detail = []
        if extras:
            detail.append("undeclared file(s): %s" % ", ".join(extras))
        if missing:
            detail.append("missing file(s): %s" % ", ".join(missing))
        raise ValueError("manifest.files is not exactly equal to the generation "
                         "contents; " + "; ".join(detail))

    # Per-file classification + content hash.
    for rel, entry in files.items():
        if not isinstance(entry, dict):
            raise ValueError("manifest.files[%s] must be an object" % rel)
        _require_exact_keys(entry, frozenset({"sha256", "class"}), "files[%s]" % rel)
        cls = _require_str(entry.get("class"), "files[%s].class" % rel)
        if cls not in FILE_CLASSES:
            raise ValueError("files[%s] has unknown class %r" % (rel, cls))
        expected = _require_sha256(entry.get("sha256"), "files[%s].sha256" % rel)
        actual_hash = _sha256_file(os.path.join(root, rel))
        if actual_hash != expected:
            raise ValueError("files[%s] sha256 mismatch" % rel)

    # Required authority must be present and classified authority/secret/config.
    for rel in _REQUIRED_AUTHORITY:
        if rel not in files:
            raise ValueError("required authority file missing from manifest: %s" % rel)
        if files[rel]["class"] not in ("authority", "secret", "config"):
            raise ValueError("authority file %s must be authority/secret/config, "
                             "not %s" % (rel, files[rel]["class"]))

    if not any(path in files for path in (
            "secrets/menhir/openai-api-key", "secrets/menhir/gemini-api-key",
            "secrets/menhir/local-llm-api-key")):
        raise ValueError("generation must include the selected provider credential authority")

    _require_sha256(manifest.get("sha256sums_sha256"), "manifest.sha256sums_sha256")
    # The hash list must itself be bound by the manifest.
    sha256sums_actual = _sha256_file(os.path.join(root, "SHA256SUMS"))
    if sha256sums_actual != manifest["sha256sums_sha256"]:
        raise ValueError("manifest.sha256sums_sha256 does not bind SHA256SUMS")
    return manifest


# ---------------------------------------------------------------------------
# release.json
# ---------------------------------------------------------------------------

def validate_release(path: str) -> dict:
    """Validate the immutable root-owned release authority record."""
    release = load_strict(path)
    if not isinstance(release, dict):
        raise ValueError("release.json must be a JSON object")
    _require_exact_keys(release, _RELEASE_TOP_KEYS, "release.json")
    if release.get("schema") != SCHEMA_VERSION:
        raise ValueError("release.json schema must be %d" % SCHEMA_VERSION)
    _require_release_id(release.get("release_id"), "release_id")
    release_author = _require_str(release.get("release_author"), "release_author")
    if len(release_author) > 128 or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._@+-]*", release_author
    ):
        raise ValueError("release_author must be a safe bounded identity")

    repos = release.get("repos")
    _require_exact_keys(repos, _RELEASE_REPOS, "repos")
    for repo in sorted(_RELEASE_REPOS):
        if not _COMMIT_RE.match(_require_str(repos.get(repo), "repos.%s" % repo)):
            raise ValueError("repos.%s must be a 40-char lowercase hex commit" % repo)
    repo_remotes = release.get("repo_remotes")
    _require_exact_keys(repo_remotes, _RELEASE_REPOS, "repo_remotes")
    for repo, expected in EXPECTED_REPO_REMOTES.items():
        if repo_remotes.get(repo) != expected:
            raise ValueError("repo_remotes.%s is not the canonical repository identity" % repo)
    deployment = release.get("deployment")
    _require_exact_keys(deployment, _RELEASE_DEPLOYMENT, "deployment")
    if deployment != _EXPECTED_DEPLOYMENT:
        raise ValueError("deployment must be the reviewed same-host Docker topology")

    _require_sha256(release.get("oauth_wheel_sha256"), "oauth_wheel_sha256")
    wheel_source = release.get("oauth_wheel_source")
    _require_exact_keys(
        wheel_source, _RELEASE_OAUTH_WHEEL_SOURCE, "oauth_wheel_source"
    )
    if wheel_source.get("repository") != "archolith_oauth":
        raise ValueError("oauth_wheel_source.repository must be archolith_oauth")
    if wheel_source.get("commit") != repos["archolith_oauth"]:
        raise ValueError("OAuth wheel source commit differs from repository authority")
    _require_sha256(
        wheel_source.get("source_tree_sha256"),
        "oauth_wheel_source.source_tree_sha256",
    )
    if wheel_source.get("wheel_sha256") != release["oauth_wheel_sha256"]:
        raise ValueError("OAuth wheel source binding has a different wheel digest")

    images = release.get("images")
    _require_exact_keys(images, _RELEASE_IMAGES, "images")
    for img in sorted(_RELEASE_IMAGES):
        _require_digest(images.get(img), "images.%s" % img)

    _require_sha256(release.get("wheel_manifest_sha256"), "wheel_manifest_sha256")
    # Dockerfile wheel-hash manifest is mandatory (blocker 7).
    _require_sha256(release.get("dockerfile_wheel_manifest_sha256"),
                    "dockerfile_wheel_manifest_sha256")
    _require_sha256(release.get("sbom_sha256"), "sbom_sha256")
    _require_sha256(release.get("scan_evidence_sha256"), "scan_evidence_sha256")
    _require_sha256(release.get("provenance_sha256"), "provenance_sha256")

    rendered = release.get("rendered")
    _require_exact_keys(rendered, _RELEASE_RENDERED, "rendered")
    for key in sorted(_RELEASE_RENDERED):
        _require_sha256(rendered.get(key), "rendered.%s" % key)

    network = release.get("network")
    _require_exact_keys(network, _RELEASE_NETWORK, "network")
    _require_str(network.get("project"), "network.project")
    _require_str(network.get("external_network"), "network.external_network")
    _require_str(network.get("alias"), "network.alias")
    peers = network.get("peers")
    if not isinstance(peers, list) or not peers or \
            any(not isinstance(p, str) or not p for p in peers):
        raise ValueError("network.peers must be a non-empty list of strings")

    rollback = release.get("rollback_anchors")
    _require_exact_keys(rollback, _RELEASE_ROLLBACK, "rollback_anchors")
    initial_release = rollback.get("initial_release")
    if not isinstance(initial_release, bool):
        raise ValueError("rollback_anchors.initial_release must be a boolean")
    prior_id = rollback.get("prior_release_id")
    if not isinstance(prior_id, str):
        raise ValueError("rollback_anchors.prior_release_id must be a string")
    if initial_release and prior_id:
        raise ValueError("initial release must have an empty prior_release_id")
    if not initial_release and not prior_id:
        raise ValueError("non-initial release must have a prior_release_id")
    # Full-rollback authority: the complete prior release record digest, not
    # merely its id, so a later rollback can verify it restores the exact prior
    # authority even if the on-disk prior record is missing or replaced.
    prior_digest = rollback.get("prior_release_sha256")
    if not isinstance(prior_digest, str):
        raise ValueError("rollback_anchors.prior_release_sha256 must be a string")
    if initial_release:
        if prior_digest:
            raise ValueError("initial release must have an empty prior_release_sha256")
    else:
        if not _SHA256_RE.match(prior_digest):
            raise ValueError("non-initial release must pin a prior_release_sha256")
    prior_images = rollback.get("prior_images")
    _require_exact_keys(prior_images, _RELEASE_PRIOR_IMAGES,
                        "rollback_anchors.prior_images")
    for image in sorted(_RELEASE_PRIOR_IMAGES):
        _require_digest(prior_images.get(image),
                        "rollback_anchors.prior_images.%s" % image)
    _require_sha256(rollback.get("prior_route_sha256"),
                    "rollback_anchors.prior_route_sha256")
    initial_host_sha = rollback.get("initial_host_state_sha256")
    if not isinstance(initial_host_sha, str):
        raise ValueError("rollback_anchors.initial_host_state_sha256 must be a string")
    if initial_release:
        _require_sha256(initial_host_sha,
                        "rollback_anchors.initial_host_state_sha256")
    elif initial_host_sha:
        raise ValueError("non-initial release must not carry initial_host_state_sha256")

    secret_versions = release.get("secret_version_ids")
    _require_exact_keys(secret_versions, _RELEASE_SECRET_VERSIONS,
                        "secret_version_ids")
    for secret in sorted(_RELEASE_SECRET_VERSIONS):
        _require_str(secret_versions.get(secret), "secret_version_ids.%s" % secret)

    artifacts = release.get("artifacts")
    if not isinstance(artifacts, dict) or not artifacts:
        raise ValueError("artifacts must be a non-empty object")
    for path, entry in artifacts.items():
        if not isinstance(path, str) or not path.startswith(("/srv/", "/etc/", "/usr/local/sbin/")):
            raise ValueError("artifacts path must be an approved absolute production path")
        if ".." in path.split("/") or path.endswith("/"):
            raise ValueError("artifacts path is not canonical: %r" % path)
        if not isinstance(entry, dict):
            raise ValueError("artifacts[%s] must be an object" % path)
        kind = entry.get("kind")
        if kind == "git":
            _require_exact_keys(entry, _RELEASE_GIT_ARTIFACT_ENTRY,
                                "artifacts[%s]" % path)
            _require_sha256(entry.get("sha256"), "artifacts[%s].sha256" % path)
            repository = entry.get("repository")
            if repository not in _RELEASE_REPOS:
                raise ValueError("artifacts[%s].repository is unknown" % path)
            if entry.get("commit") != repos[repository]:
                raise ValueError("artifacts[%s].commit differs from repository authority" % path)
            source_path = _require_str(entry.get("path"), "artifacts[%s].path" % path)
            if source_path.startswith("/") or ".." in source_path.split("/"):
                raise ValueError("artifacts[%s].path is not a canonical repo path" % path)
            blob_oid = _require_str(entry.get("blob_oid"), "artifacts[%s].blob_oid" % path)
            if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", blob_oid):
                raise ValueError("artifacts[%s].blob_oid is invalid" % path)
        elif kind == "rendered":
            _require_exact_keys(entry, _RELEASE_RENDERED_ARTIFACT_ENTRY,
                                "artifacts[%s]" % path)
            digest = _require_sha256(entry.get("sha256"),
                                     "artifacts[%s].sha256" % path)
            rendered_key = entry.get("rendered_key")
            if rendered_key not in _RELEASE_RENDERED or rendered.get(rendered_key) != digest:
                raise ValueError("artifacts[%s] is not bound to rendered authority" % path)
        else:
            raise ValueError("artifacts[%s].kind must be git or rendered" % path)
    for path, rendered_key in _RELEASE_REQUIRED_RENDERED_ARTIFACTS.items():
        entry = artifacts.get(path)
        if not isinstance(entry, dict) or entry.get("kind") != "rendered" \
                or entry.get("rendered_key") != rendered_key \
                or entry.get("sha256") != rendered[rendered_key]:
            raise ValueError(
                "artifacts[%s] is a required rendered artifact bound to %s"
                % (path, rendered_key)
            )

    review = release.get("security_review")
    _require_exact_keys(review, _RELEASE_SECURITY_REVIEW_KEYS, "security_review")
    if review.get("schema") != 1 \
            or review.get("kind") != "menhir-production-security-review":
        raise ValueError("security_review kind/schema is invalid")
    _require_key_id(review.get("review_id"), "security_review.review_id")
    if review.get("release_author") != release_author:
        raise ValueError("security_review release_author differs from release authority")
    reviewer = _require_str(review.get("reviewer"), "security_review.reviewer")
    if len(reviewer) > 128 or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._@+-]*", reviewer
    ):
        raise ValueError("security_review.reviewer must be a safe bounded identity")
    if reviewer.casefold() == release_author.casefold():
        raise ValueError("security reviewer must be independent from release author")
    reviewed_utc = _parse_utc(
        review.get("reviewed_utc"), "security_review.reviewed_utc"
    )
    if reviewed_utc > datetime.now(timezone.utc) + timedelta(seconds=60):
        raise ValueError("security_review.reviewed_utc is in the future")
    if review.get("verdict") != "APPROVED":
        raise ValueError("security_review verdict must be APPROVED")
    findings = review.get("unresolved_findings")
    _require_exact_keys(
        findings, _RELEASE_SECURITY_FINDINGS,
        "security_review.unresolved_findings",
    )
    for severity in sorted(_RELEASE_SECURITY_FINDINGS):
        count = findings.get(severity)
        if isinstance(count, bool) or not isinstance(count, int) or count != 0:
            raise ValueError(
                "security_review unresolved %s findings must be zero" % severity
            )
    scope = review.get("scope")
    if not isinstance(scope, list) \
            or any(not isinstance(item, str) for item in scope) \
            or len(scope) != len(set(scope)) \
            or set(scope) != REQUIRED_SECURITY_REVIEW_SCOPE:
        raise ValueError("security_review scope must cover every required security area")
    _require_sha256(review.get("report_sha256"), "security_review.report_sha256")
    _require_sha256(
        review.get("review_artifact_sha256"),
        "security_review.review_artifact_sha256",
    )
    authority_sha = _require_sha256(
        review.get("authority_sha256"), "security_review.authority_sha256"
    )
    if authority_sha != release_authority_sha256(release):
        raise ValueError("security_review does not bind the exact release authority")
    return release


def _validate_release_binding(document: dict, label: str) -> None:
    _require_str(document.get("release_id"), "%s.release_id" % label)
    _require_sha256(document.get("release_manifest_sha256"),
                    "%s.release_manifest_sha256" % label)


def validate_prerequisite(path: str) -> dict:
    """Validate independently signed public-network prerequisite observations."""
    receipt = load_strict(path)
    _require_exact_keys(receipt, _PREREQUISITE_KEYS, "prerequisite receipt")
    if receipt.get("schema") != SCHEMA_VERSION:
        raise ValueError("prerequisite receipt schema must be %d" % SCHEMA_VERSION)
    if receipt.get("kind") != "external-prerequisite":
        raise ValueError("prerequisite receipt kind must be external-prerequisite")
    _validate_release_binding(receipt, "prerequisite receipt")
    _require_fresh(receipt.get("checked_utc"), "prerequisite receipt.checked_utc", 900)
    route_version = _require_str(receipt.get("route_version"), "route_version")
    observations = receipt.get("observations")
    if not isinstance(observations, list) or len(observations) < 2:
        raise ValueError("prerequisite receipt requires at least two observations")
    workers = set()
    networks = set()
    for observation in observations:
        _require_exact_keys(
            observation, _PREREQUISITE_OBSERVATION_KEYS,
            "prerequisite observation",
        )
        worker = _require_key_id(observation.get("worker_id"), "worker_id")
        network = _require_key_id(observation.get("network_id"), "network_id")
        if worker in workers or network in networks:
            raise ValueError("prerequisite observations must use distinct workers and networks")
        workers.add(worker); networks.add(network)
        _require_fresh(observation.get("observed_utc"), "observed_utc", 900)
        if observation.get("route_version") != route_version:
            raise ValueError("prerequisite observation route_version mismatch")
        checks = observation.get("checks")
        _require_exact_keys(checks, _PREREQUISITE_CHECKS, "prerequisite checks")
        if any(checks.get(key) is not True for key in _PREREQUISITE_CHECKS):
            raise ValueError("every prerequisite observation check must be true")
        _decode_b64url(observation.get("signature"), "observation signature", 64)
    return receipt


def prerequisite_observation_payload(receipt: dict, observation: dict) -> bytes:
    value = {
        "release_id": receipt["release_id"],
        "release_manifest_sha256": receipt["release_manifest_sha256"],
        "worker_id": observation["worker_id"],
        "network_id": observation["network_id"],
        "observed_utc": observation["observed_utc"],
        "route_version": observation["route_version"],
        "checks": observation["checks"],
    }
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("ascii")


def source_fence_payload(receipt: dict) -> str:
    """Deterministic canonical payload covered by the source Ed25519 signature.

    The signature binds every signed claim (release identity, the exact fenced
    source identity, and the active window) so a tampered or re-substituted
    fence cannot be passed off as belonging to this release/source.
    """
    signed = {key: receipt[key] for key in sorted(_SOURCE_FENCE_SIGNED)
              if key in receipt}
    return json.dumps(signed, sort_keys=True, separators=(",", ":"))


def _decode_b64url(value: object, label: str, expected_length: int) -> bytes:
    import base64
    if not isinstance(value, str) or not value or "=" in value or not _B64URL_RE.fullmatch(value):
        raise ValueError("%s must be canonical unpadded base64url" % label)
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except Exception as exc:
        raise ValueError("%s is invalid base64url" % label) from exc
    canonical = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    if len(raw) != expected_length or canonical != value:
        raise ValueError("%s has invalid length or encoding" % label)
    return raw


def _decode_ed25519_public_key(value: object, label: str) -> bytes:
    return _decode_b64url(value, label, 32)


def verify_source_fence(path: str, release_path: str) -> dict:
    """Validate a receipt signed by the source-only Ed25519 private key."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    receipt = validate_source_fence(path)
    release = validate_release(release_path)
    if receipt["release_id"] != release["release_id"]:
        raise ValueError("source-fence release_id differs from release authority")
    if receipt["release_manifest_sha256"] != _sha256_file(release_path):
        raise ValueError("source-fence release digest differs from release authority")
    expected_key_id = release["source_fence_key_id"]
    if receipt["signing_key_id"] != expected_key_id:
        raise ValueError("source-fence signing_key_id differs from release authority")
    public_key = _decode_ed25519_public_key(
        release["source_fence_public_key"], "source_fence_public_key"
    )
    signature = _decode_b64url(receipt["signature"], "source-fence signature", 64)
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(
            signature, source_fence_payload(receipt).encode("utf-8")
        )
    except Exception as exc:
        raise ValueError("source-fence signature is invalid") from exc
    return receipt


def validate_source_fence(path: str) -> dict:
    """Validate evidence that the old/local authority can no longer write."""
    receipt = load_strict(path)
    _require_exact_keys(receipt, _SOURCE_FENCE_KEYS, "source-fence receipt")
    if receipt.get("schema") != SCHEMA_VERSION:
        raise ValueError("source-fence receipt schema must be %d" % SCHEMA_VERSION)
    if receipt.get("kind") != "source-writer-fence":
        raise ValueError("source-fence receipt kind must be source-writer-fence")
    _validate_release_binding(receipt, "source-fence receipt")
    checked = _require_fresh(receipt.get("checked_utc"), "source-fence receipt.checked_utc", 300)
    expires = _parse_utc(receipt.get("expires_utc"), "source-fence receipt.expires_utc")
    now = datetime.now(timezone.utc)
    if expires <= now or expires > checked + timedelta(minutes=10):
        raise ValueError(
            "source-fence receipt expires outside the active window; "
            "expiry may be no more than 10 minutes after check"
        )
    _require_str(receipt.get("source_id"), "source-fence receipt.source_id")
    for key in ("source_writer_stopped", "source_mutation_probe_denied",
                "source_service_disabled", "source_firewall_persistent"):
        if receipt.get(key) is not True:
            raise ValueError("source-fence receipt.%s must be true" % key)
    _require_key_id(receipt.get("signing_key_id"),
                    "source-fence receipt.signing_key_id")
    signature = receipt.get("signature")
    _decode_b64url(signature, "source-fence signature", 64)
    return receipt


# ---------------------------------------------------------------------------
# Receipts
# ---------------------------------------------------------------------------

_RECEIPT_KINDS = frozenset({"backup-local", "rehearsal", "candidate-accept"})

_RECEIPT_RELEASE_KEYS = frozenset(
    {"release_id", "release_manifest_sha256", "menhir_image_digest",
     "neo4j_image_digest"}
)

_LOCAL_ENCRYPTION_KEYS = frozenset({
    "algorithm", "recipient", "plaintext_archive_sha256", "roundtrip_verified",
})

_LOCAL_ARCHIVES_KEYS = frozenset({
    "retention_target_generations", "retained_generation_count",
    "current_archive_path", "archives",
})

_LOCAL_ARCHIVE_ENTRY_KEYS = frozenset({
    "generation", "path", "sha256", "size",
})

_DESKTOP_ARCHIVE_KEYS = frozenset({
    "schema", "kind", "generation", "release", "archive",
    "desktop_destination", "archived_utc",
})
_DESKTOP_RELEASE_KEYS = frozenset({"release_id", "release_manifest_sha256"})
_DESKTOP_ARCHIVE_ENTRY_KEYS = frozenset({"sha256", "size_bytes"})


def _validate_receipt_release(release, label):
    _require_exact_keys(release, _RECEIPT_RELEASE_KEYS, label)
    _require_str(release.get("release_id"), "%s.release_id" % label)
    _require_sha256(release.get("release_manifest_sha256"),
                    "%s.release_manifest_sha256" % label)
    _require_digest(release.get("menhir_image_digest"),
                    "%s.menhir_image_digest" % label)
    _require_digest(release.get("neo4j_image_digest"),
                    "%s.neo4j_image_digest" % label)


def validate_receipt(path: str, kind: str) -> dict:
    """Validate a structured lifecycle receipt of the given kind."""
    if kind not in _RECEIPT_KINDS:
        raise ValueError("unknown receipt kind: %r" % kind)
    receipt = load_strict(path)
    if not isinstance(receipt, dict):
        raise ValueError("receipt must be a JSON object")
    if receipt.get("schema") != SCHEMA_VERSION:
        raise ValueError("receipt schema must be %d" % SCHEMA_VERSION)
    if receipt.get("kind") != kind:
        raise ValueError("receipt kind %r != expected %r" % (receipt.get("kind"), kind))
    generation = _require_str(receipt.get("generation"), "receipt.generation")
    if not _GENERATION_RE.match(generation):
        raise ValueError("receipt.generation is invalid")
    _require_sha256(receipt.get("manifest_sha256"), "receipt.manifest_sha256")
    _validate_receipt_release(receipt.get("release"), "receipt.release")

    if kind == "backup-local":
        _require_exact_keys(receipt, frozenset({
            "schema", "kind", "operation_job_id", "generation",
            "manifest_sha256", "release",
            "encryption", "local_encrypted_archives", "plaintext_removed", "checked_utc",
        }), "backup-local receipt")
        operation_job_id = _require_str(
            receipt.get("operation_job_id"), "backup-local.operation_job_id"
        )
        if not _OPERATION_JOB_ID_RE.match(operation_job_id):
            raise ValueError("backup-local.operation_job_id is invalid")
        encryption = receipt.get("encryption")
        _require_exact_keys(encryption, _LOCAL_ENCRYPTION_KEYS, "encryption")
        if encryption.get("algorithm") != "age-x25519":
            raise ValueError("encryption.algorithm must be age-x25519")
        recipient = _require_str(encryption.get("recipient"), "encryption.recipient")
        if not recipient.startswith("age1"):
            raise ValueError("encryption.recipient must be an age recipient")
        _require_sha256(encryption.get("plaintext_archive_sha256"),
                        "encryption.plaintext_archive_sha256")
        if encryption.get("roundtrip_verified") is not True:
            raise ValueError("encryption.roundtrip_verified must be true")

        local_archives = receipt.get("local_encrypted_archives")
        _require_exact_keys(local_archives, _LOCAL_ARCHIVES_KEYS,
                            "local_encrypted_archives")
        retention_target = local_archives.get("retention_target_generations")
        if not isinstance(retention_target, int) or isinstance(retention_target, bool) \
                or retention_target < 1:
            raise ValueError("local encrypted retention target must be positive")
        archives = local_archives.get("archives")
        if not isinstance(archives, list) or not archives:
            raise ValueError("local_encrypted_archives.archives must be a non-empty list")
        archive_generations = set()
        archive_paths = set()
        current_matches = []
        current_archive_path = _require_str(
            local_archives.get("current_archive_path"),
            "local_encrypted_archives.current_archive_path",
        )
        if not (posixpath.isabs(current_archive_path) or ntpath.isabs(current_archive_path)):
            raise ValueError("local_encrypted_archives.current_archive_path must be absolute")
        for index, archive in enumerate(archives):
            label = "local_encrypted_archives.archives[%d]" % index
            _require_exact_keys(archive, _LOCAL_ARCHIVE_ENTRY_KEYS, label)
            archive_generation = _require_str(archive.get("generation"),
                                              "%s.generation" % label)
            if not _GENERATION_RE.match(archive_generation):
                raise ValueError("%s.generation is invalid" % label)
            local_path = _require_str(archive.get("path"), "%s.path" % label)
            if not (posixpath.isabs(local_path) or ntpath.isabs(local_path)):
                raise ValueError("%s.path must be absolute" % label)
            if not os.path.basename(local_path).startswith(archive_generation + "-") or \
                    not local_path.endswith(".tar.gz.age"):
                raise ValueError("%s.path must bind its generation archive name" % label)
            if local_path in archive_paths:
                raise ValueError("local encrypted archive paths must be unique")
            archive_paths.add(local_path)
            archive_generations.add(archive_generation)
            archive_sha256 = _require_sha256(archive.get("sha256"),
                                             "%s.sha256" % label)
            if not isinstance(archive.get("size"), int) or \
                    isinstance(archive.get("size"), bool) or archive["size"] <= 0:
                raise ValueError("%s.size must be positive" % label)
            if archive_generation == generation and \
                    local_path == current_archive_path:
                current_matches.append(archive)
        retained_count = local_archives.get("retained_generation_count")
        if not isinstance(retained_count, int) or isinstance(retained_count, bool) or \
                retained_count != len(archive_generations):
            raise ValueError("local retained generation count must match distinct evidence")
        if retained_count < retention_target:
            raise ValueError("local retained generation count is below retention target")
        if not current_matches:
            raise ValueError(
                "local encrypted archive evidence must include the current generation"
            )
        # Pending receipts (written before plaintext removal) carry False;
        # only a finalized receipt with True may gate promotion/retirement.
        if not isinstance(receipt.get("plaintext_removed"), bool):
            raise ValueError("plaintext_removed must be a boolean")
        _parse_utc(receipt.get("checked_utc"), "backup-local.checked_utc")

    elif kind == "rehearsal":
        _require_exact_keys(receipt, frozenset({
            "schema", "kind", "generation", "manifest_sha256", "release",
            "neo4j_check", "sqlite_integrity", "checked_utc",
        }), "rehearsal receipt")
        if receipt.get("neo4j_check") != "ok":
            raise ValueError("rehearsal.neo4j_check must be 'ok'")
        if receipt.get("sqlite_integrity") != "ok":
            raise ValueError("rehearsal.sqlite_integrity must be 'ok'")
        _require_fresh(receipt.get("checked_utc"), "rehearsal.checked_utc", 86400)

    elif kind == "candidate-accept":
        _require_exact_keys(receipt, frozenset({
            "schema", "kind", "generation", "manifest_sha256", "release",
            "readyz", "oauth_discovery", "recall", "mutation_503",
            "tier_tool_identity", "authority_before_digest",
            "authority_after_digest", "same_host_writer_fence_sha256",
            "checked_utc",
        }), "candidate-accept receipt")
        for key in ("readyz", "oauth_discovery", "mutation_503",
                    "tier_tool_identity"):
            if receipt.get(key) != "ok":
                raise ValueError("candidate-accept.%s must be 'ok'" % key)
        if receipt.get("recall") not in ("ok", "skipped"):
            raise ValueError("candidate-accept.recall must be 'ok' or 'skipped'")
        _require_sha256(receipt.get("authority_before_digest"),
                        "authority_before_digest")
        _require_sha256(receipt.get("authority_after_digest"),
                        "authority_after_digest")
        _require_sha256(receipt.get("same_host_writer_fence_sha256"),
                        "same_host_writer_fence_sha256")
        _require_fresh(receipt.get("checked_utc"), "candidate-accept.checked_utc", 900)

    return receipt


def validate_receipt_binding(path: str, kind: str, release_path: str,
                             generation: str, manifest_sha256: str,
                             menhir_digest: str, neo4j_digest: str) -> dict:
    """Validate a receipt and bind it to one exact release and generation."""
    receipt = validate_receipt(path, kind)
    release = validate_release(release_path)
    if receipt.get("generation") != generation:
        raise ValueError("receipt.generation does not match expected value")
    if receipt.get("manifest_sha256") != _require_sha256(
            manifest_sha256, "expected manifest sha256"):
        raise ValueError("receipt.manifest_sha256 does not match expected value")
    binding = receipt["release"]
    checks = {
        "release_id": release["release_id"],
        "release_manifest_sha256": _sha256_file(release_path),
        "menhir_image_digest": _require_digest(menhir_digest, "expected Menhir digest"),
        "neo4j_image_digest": _require_digest(neo4j_digest, "expected Neo4j digest"),
    }
    for key, value in checks.items():
        if binding.get(key) != value:
            raise ValueError("receipt.release.%s does not match release authority" % key)
    if release["images"]["menhir"] != menhir_digest:
        raise ValueError("expected Menhir digest differs from release authority")
    if release["images"]["neo4j"] != neo4j_digest:
        raise ValueError("expected Neo4j digest differs from release authority")
    return receipt


def validate_backup_promotion(
        path: str, archive_root: str = "/srv/menhir/backups/encrypted") -> dict:
    """Validate promotion evidence against the encrypted files that exist now."""
    receipt = validate_receipt(path, "backup-local")
    if receipt.get("plaintext_removed") is not True:
        raise ValueError("plaintext removal must be confirmed before promotion")
    local_archives = receipt["local_encrypted_archives"]
    if local_archives["retention_target_generations"] < 2:
        raise ValueError("promotion requires two retained encrypted generations")
    _require_fresh(receipt["checked_utc"], "backup-local.checked_utc", 3600)
    archive_root = os.path.abspath(archive_root)
    root_stat = os.lstat(archive_root)
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise ValueError("encrypted archive root must be a non-symlink directory")
    archives = local_archives["archives"]
    for index, archive in enumerate(archives):
        archive_path = os.path.abspath(archive["path"])
        if os.path.dirname(archive_path) != archive_root:
            raise ValueError("encrypted archive is outside the fixed archive root")
        info = os.lstat(archive_path)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ValueError("encrypted archive must be a regular non-symlink file")
        if info.st_size != archive["size"]:
            raise ValueError("encrypted archive size does not match receipt")
        if _sha256_file(archive_path) != archive["sha256"]:
            raise ValueError("encrypted archive digest does not match receipt")
    return receipt


def validate_desktop_archive(
        path: str, backup_path: str, release_path: str,
        archive_root: str = "/srv/menhir/backups/encrypted") -> dict:
    """Bind a fresh desktop-copy receipt to the live backup and release authority."""
    receipt = load_strict(path)
    _require_exact_keys(receipt, _DESKTOP_ARCHIVE_KEYS, "desktop archive receipt")
    if receipt.get("schema") != SCHEMA_VERSION:
        raise ValueError("desktop archive receipt schema must be %d" % SCHEMA_VERSION)
    if receipt.get("kind") != "menhir-desktop-archive":
        raise ValueError("desktop archive receipt kind is invalid")
    generation = _require_str(receipt.get("generation"), "desktop archive generation")
    if not _GENERATION_RE.match(generation):
        raise ValueError("desktop archive generation is invalid")
    release_binding = receipt.get("release")
    _require_exact_keys(release_binding, _DESKTOP_RELEASE_KEYS, "desktop archive release")
    _require_str(release_binding.get("release_id"), "desktop archive release_id")
    _require_sha256(release_binding.get("release_manifest_sha256"),
                    "desktop archive release manifest")
    archive = receipt.get("archive")
    _require_exact_keys(archive, _DESKTOP_ARCHIVE_ENTRY_KEYS, "desktop archive")
    _require_sha256(archive.get("sha256"), "desktop archive sha256")
    size = archive.get("size_bytes")
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        raise ValueError("desktop archive size_bytes must be positive")
    destination = _require_str(
        receipt.get("desktop_destination"), "desktop archive destination"
    )
    if not ntpath.isabs(destination):
        raise ValueError("desktop archive destination must be an absolute Windows path")
    _require_fresh(receipt.get("archived_utc"), "desktop archive archived_utc", 3600)

    backup = validate_backup_promotion(backup_path, archive_root)
    release = validate_release(release_path)
    if backup["release"]["release_id"] != release["release_id"] or \
            backup["release"]["release_manifest_sha256"] != _sha256_file(release_path):
        raise ValueError("backup receipt does not bind the release authority")
    current_path = backup["local_encrypted_archives"]["current_archive_path"]
    current = next(
        item for item in backup["local_encrypted_archives"]["archives"]
        if item["path"] == current_path and item["generation"] == backup["generation"]
    )
    expected = {
        "generation": backup["generation"],
        "release_id": release["release_id"],
        "release_manifest_sha256": _sha256_file(release_path),
        "sha256": current["sha256"],
        "size_bytes": current["size"],
    }
    actual = {
        "generation": generation,
        "release_id": release_binding["release_id"],
        "release_manifest_sha256": release_binding["release_manifest_sha256"],
        "sha256": archive["sha256"],
        "size_bytes": size,
    }
    if actual != expected:
        raise ValueError("desktop archive receipt does not bind the current backup and release")
    return receipt


def validate_prerequisite_binding(path: str, release_path: str) -> dict:
    """Validate and bind an external prerequisite receipt to one release."""
    receipt = validate_prerequisite(path)
    release = validate_release(release_path)
    if receipt["release_id"] != release["release_id"]:
        raise ValueError("prerequisite release_id mismatch")
    if receipt["release_manifest_sha256"] != _sha256_file(release_path):
        raise ValueError("prerequisite release digest mismatch")
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    keys = release["external_evidence_public_keys"]
    for observation in receipt["observations"]:
        worker_id = observation["worker_id"]
        if worker_id not in keys:
            raise ValueError("external observation worker is not release-pinned")
        public = _decode_ed25519_public_key(keys[worker_id], "external worker public key")
        signature = _decode_b64url(observation["signature"], "observation signature", 64)
        try:
            Ed25519PublicKey.from_public_bytes(public).verify(
                signature, prerequisite_observation_payload(receipt, observation)
            )
        except Exception as exc:
            raise ValueError("external observation signature is invalid") from exc
    return receipt


def main(argv):
    if len(argv) < 3:
        print("usage: menhir_schema.py <validate-manifest|validate-release|"
              "validate-receipt|validate-receipt-binding|validate-prerequisite|"
              "validate-prerequisite-binding|validate-source-fence|"
              "verify-source-fence|validate-backup-promotion|"
              "validate-desktop-archive> "
               "<path> [root] [kind] [release_path]", file=sys.stderr)
        return 2
    command, path = argv[1], argv[2]
    try:
        if command == "validate-manifest":
            validate_manifest(path, argv[3])
        elif command == "validate-release":
            validate_release(path)
        elif command == "validate-receipt":
            validate_receipt(path, argv[3])
        elif command == "validate-receipt-binding":
            validate_receipt_binding(path, argv[3], argv[4], argv[5], argv[6],
                                     argv[7], argv[8])
        elif command == "validate-prerequisite":
            validate_prerequisite(path)
        elif command == "validate-prerequisite-binding":
            validate_prerequisite_binding(path, argv[3])
        elif command == "validate-source-fence":
            validate_source_fence(path)
        elif command == "verify-source-fence":
            if len(argv) < 4:
                raise ValueError("verify-source-fence requires release path")
            verify_source_fence(path, argv[3])
        elif command == "validate-backup-promotion":
            validate_backup_promotion(path)
        elif command == "validate-desktop-archive":
            if len(argv) != 5:
                raise ValueError(
                    "validate-desktop-archive requires backup and release paths"
                )
            validate_desktop_archive(path, argv[3], argv[4])
        else:
            print("unknown command: %s" % command, file=sys.stderr)
            return 2
    except (ValueError, OSError) as exc:
        print("validation failed: %s" % exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
