"""release.json validation for the immutable Menhir release authority record.

Extracted from ``menhir_schema``: the release authority label-shape constants
(historical and current top-key sets, publication, security-review, rollback,
artifact contracts) and ``validate_release``. Unknown labels are rejected,
image pins must be digest-pinned, and the Dockerfile wheel-hash manifest is
mandatory. Import through ``menhir_schema``.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from menhir_schema_support import (
    SCHEMA_VERSION,
    _COMMIT_RE,
    _SHA256_RE,
    _parse_utc,
    _require_digest,
    _require_exact_keys,
    _require_key_id,
    _require_release_id,
    _require_sha256,
    _require_str,
    load_strict,
    release_authority_sha256,
)

_LEGACY_RELEASE_TOP_KEYS = frozenset({
    "schema", "release_id", "release_author", "security_review", "repos",
    "oauth_wheel_sha256", "oauth_wheel_source", "images",
    "wheel_manifest_sha256", "dockerfile_wheel_manifest_sha256",
    "sbom_sha256", "scan_evidence_sha256", "provenance_sha256",
    "rendered", "network", "rollback_anchors", "secret_version_ids",
    "artifacts", "repo_remotes", "deployment",
})
_PRE_INGRESS_RELEASE_TOP_KEYS = _LEGACY_RELEASE_TOP_KEYS | frozenset({
    "deployment_class", "notes_json_sha256", "notes_markdown_sha256",
})
_PRE_IMAGE_PUBLICATION_RELEASE_TOP_KEYS = (
    _PRE_INGRESS_RELEASE_TOP_KEYS | frozenset({"ingress_mode"})
)
_PRE_IMAGE_REFS_RELEASE_TOP_KEYS = (
    _PRE_IMAGE_PUBLICATION_RELEASE_TOP_KEYS | frozenset({"image_publication"})
)
_RELEASE_TOP_KEYS = _PRE_IMAGE_REFS_RELEASE_TOP_KEYS | frozenset({"image_refs"})
_PRE_ATTESTATION_IMAGE_PUBLICATION_KEYS = frozenset({
    "publication_sha256", "validation_identity_sha256", "image_id",
    "config_sha256", "image_archive_sha256", "registry_digest",
})
_RELEASE_IMAGE_PUBLICATION_KEYS = frozenset({
    "publication_sha256", "source_repository", "source_commit",
    "validation_identity_sha256", "image_id", "config_sha256",
    "image_archive_sha256", "registry_digest", "sbom_sha256",
    "scan_evidence_sha256", "attestation_bundle_sha256",
    "attestation_trusted_root_sha256",
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
_RELEASE_REPOS = frozenset({"menhir", "archolith_oauth", "yawn_deploy"})
# yawn_vps contributed host artifacts until release 0.2.0-16 and stayed pinned
# there for provenance; records up to 16 carry it, records from 17 do not.
_LEGACY_RELEASE_REPOS = _RELEASE_REPOS | frozenset({"yawn_vps"})
_RELEASE_IMAGES = frozenset({"menhir", "neo4j", "caddy", "base"})
_RELEASE_IMAGE_REFS = _RELEASE_IMAGES
_RELEASE_DEPLOYMENT_CLASSES = frozenset({
    "app-only", "security-config", "maintenance",
})
_RELEASE_INGRESS_MODES = frozenset({"cloudflared"})
_RELEASE_RENDERED = frozenset({
    "menhir_compose_sha256", "yawn_compose_sha256", "caddy_sha256",
    "registry_sha256", "policy_sha256", "yawn_env_sha256",
    "production_env_sha256",
})
# The OAuth operations gateway retired in release 0.2.0-16. Records before it
# carry these three keys and their /etc/yawn-vps artifacts; records after it
# carry neither. Absent key => artifact must be absent (enforced below).
_RELEASE_RENDERED_OPTIONAL = frozenset({
    "operations_policy_sha256", "oauth_public_key_sha256",
    "python_runtime_digest_sha256",
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


def validate_release(path: str) -> dict:
    """Validate the immutable root-owned release authority record."""
    release = load_strict(path)
    if not isinstance(release, dict):
        raise ValueError("release.json must be a JSON object")
    release_keys = set(release)
    if release_keys not in {
        _RELEASE_TOP_KEYS, _PRE_IMAGE_REFS_RELEASE_TOP_KEYS,
        _PRE_IMAGE_PUBLICATION_RELEASE_TOP_KEYS,
        _PRE_INGRESS_RELEASE_TOP_KEYS, _LEGACY_RELEASE_TOP_KEYS,
    }:
        _require_exact_keys(release, _RELEASE_TOP_KEYS, "release.json")
    if release.get("schema") != SCHEMA_VERSION:
        raise ValueError("release.json schema must be %d" % SCHEMA_VERSION)
    _require_release_id(release.get("release_id"), "release_id")
    release_author = _require_str(release.get("release_author"), "release_author")
    if len(release_author) > 128 or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._@+-]*", release_author
    ):
        raise ValueError("release_author must be a safe bounded identity")
    if release_keys in {
        _RELEASE_TOP_KEYS, _PRE_IMAGE_REFS_RELEASE_TOP_KEYS,
        _PRE_IMAGE_PUBLICATION_RELEASE_TOP_KEYS,
        _PRE_INGRESS_RELEASE_TOP_KEYS,
    }:
        if release.get("deployment_class") not in _RELEASE_DEPLOYMENT_CLASSES:
            raise ValueError("deployment_class is invalid")
        _require_sha256(release.get("notes_json_sha256"), "notes_json_sha256")
        _require_sha256(
            release.get("notes_markdown_sha256"), "notes_markdown_sha256"
        )
    if release_keys in {
            _RELEASE_TOP_KEYS, _PRE_IMAGE_REFS_RELEASE_TOP_KEYS,
            _PRE_IMAGE_PUBLICATION_RELEASE_TOP_KEYS,
    } \
            and release.get("ingress_mode") not in _RELEASE_INGRESS_MODES:
        raise ValueError("ingress_mode is invalid")

    repos = release.get("repos")
    if not isinstance(repos, dict) or frozenset(repos) not in (
            _RELEASE_REPOS, _LEGACY_RELEASE_REPOS):
        raise ValueError("repos must name exactly the release repositories")
    release_repos = frozenset(repos)
    for repo in sorted(release_repos):
        if not _COMMIT_RE.match(_require_str(repos.get(repo), "repos.%s" % repo)):
            raise ValueError("repos.%s must be a 40-char lowercase hex commit" % repo)
    repo_remotes = release.get("repo_remotes")
    _require_exact_keys(repo_remotes, release_repos, "repo_remotes")
    for repo in sorted(release_repos):
        if repo_remotes.get(repo) != EXPECTED_REPO_REMOTES[repo]:
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

    if release_keys == _RELEASE_TOP_KEYS:
        image_refs = release.get("image_refs")
        _require_exact_keys(image_refs, _RELEASE_IMAGE_REFS, "image_refs")
        for image in sorted(_RELEASE_IMAGE_REFS):
            reference = _require_str(
                image_refs.get(image), "image_refs.%s" % image
            )
            if not reference.endswith("@" + images[image]) \
                    or not re.fullmatch(
                        r"[a-z0-9]+(?:[._-][a-z0-9]+)*(?::[0-9]+)?"
                        r"(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*"
                        r"@sha256:[0-9a-f]{64}", reference
                    ):
                raise ValueError(
                    "image_refs.%s must be an immutable matching reference" % image
                )

    if release_keys in {_RELEASE_TOP_KEYS, _PRE_IMAGE_REFS_RELEASE_TOP_KEYS}:
        image_publication = release.get("image_publication")
        _require_exact_keys(
            image_publication,
            _RELEASE_IMAGE_PUBLICATION_KEYS if release_keys == _RELEASE_TOP_KEYS
            else _PRE_ATTESTATION_IMAGE_PUBLICATION_KEYS,
            "image_publication",
        )
        _require_sha256(
            image_publication.get("publication_sha256"),
            "image_publication.publication_sha256",
        )
        _require_sha256(
            image_publication.get("validation_identity_sha256"),
            "image_publication.validation_identity_sha256",
        )
        _require_digest(
            image_publication.get("image_id"), "image_publication.image_id"
        )
        _require_sha256(
            image_publication.get("config_sha256"),
            "image_publication.config_sha256",
        )
        _require_sha256(
            image_publication.get("image_archive_sha256"),
            "image_publication.image_archive_sha256",
        )
        registry_digest = _require_digest(
            image_publication.get("registry_digest"),
            "image_publication.registry_digest",
        )
        if registry_digest != images["menhir"]:
            raise ValueError(
                "image_publication.registry_digest differs from images.menhir"
            )
        if release_keys == _RELEASE_TOP_KEYS:
            if image_publication.get("source_repository") != "Archolith/menhir":
                raise ValueError(
                    "image_publication.source_repository is not canonical"
                )
            if image_publication.get("source_commit") != repos["menhir"]:
                raise ValueError(
                    "image_publication.source_commit differs from repository authority"
                )
            for key in (
                "sbom_sha256", "scan_evidence_sha256",
                "attestation_bundle_sha256", "attestation_trusted_root_sha256",
            ):
                _require_sha256(
                    image_publication.get(key), "image_publication.%s" % key
                )
            if image_publication["sbom_sha256"] != release.get("sbom_sha256"):
                raise ValueError("image publication SBOM digest differs from release")
            if image_publication["scan_evidence_sha256"] != release.get(
                    "scan_evidence_sha256"):
                raise ValueError("image publication scan digest differs from release")

    _require_sha256(release.get("wheel_manifest_sha256"), "wheel_manifest_sha256")
    # Dockerfile wheel-hash manifest is mandatory (blocker 7).
    _require_sha256(release.get("dockerfile_wheel_manifest_sha256"),
                    "dockerfile_wheel_manifest_sha256")
    _require_sha256(release.get("sbom_sha256"), "sbom_sha256")
    _require_sha256(release.get("scan_evidence_sha256"), "scan_evidence_sha256")
    _require_sha256(release.get("provenance_sha256"), "provenance_sha256")

    rendered = release.get("rendered")
    if not isinstance(rendered, dict) or not _RELEASE_RENDERED.issubset(rendered) \
            or not set(rendered).issubset(_RELEASE_RENDERED | _RELEASE_RENDERED_OPTIONAL):
        raise ValueError("rendered has invalid labels")
    for key in sorted(rendered):
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
            if repository not in release_repos:
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
            if rendered_key not in (_RELEASE_RENDERED | _RELEASE_RENDERED_OPTIONAL) \
                    or rendered.get(rendered_key) != digest:
                raise ValueError("artifacts[%s] is not bound to rendered authority" % path)
        else:
            raise ValueError("artifacts[%s].kind must be git or rendered" % path)
    for path, rendered_key in _RELEASE_REQUIRED_RENDERED_ARTIFACTS.items():
        if rendered_key in _RELEASE_RENDERED_OPTIONAL and rendered_key not in rendered:
            if path in artifacts:
                raise ValueError(
                    "artifacts[%s] cannot exist without rendered authority" % path
                )
            continue
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
