#!/usr/bin/env python3
"""Author one canonical four-repository Menhir production release record."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(SCRIPT_DIR / "lib"))
sys.path.insert(0, str(SCRIPT_DIR.parent / "src"))

import menhir_schema  # noqa: E402
import release_spec  # noqa: E402
import verify_wheelhouse  # noqa: E402
from menhir.access_contract import validate_access_contract  # noqa: E402

from release_author_constants import (  # noqa: E402
    ALLOWED_ARTIFACT_PREFIXES, COMMIT_RE, DEPLOYMENT_CLASSES, DIGEST_RE,
    EVIDENCE, EXPECTED_REPO_REMOTES, IMAGES, INGRESS_MODES,
    INHERITED_EVIDENCE, LITERAL_RENDERED_DIGESTS, RENDERED,
    RENDERED_ARTIFACT_DESTINATIONS, RELEASE_ID_RE,
    REQUIRED_ARTIFACT_DESTINATIONS, REPOSITORIES, SECRET_VERSIONS,
    SECURITY_REVIEW_KEYS, SHA256_RE, SPEC_KEYS, SPEC_KEYS_INHERITED,
    SPEC_KEYS_WITH_PROVENANCE, _installed_destinations,
)
from release_author_evidence import (  # noqa: E402
    _validate_policy_env_binding, _validate_provenance,
)
from release_author_git import (  # noqa: E402
    _canonical_remote, _git_blob, _git_package_files,
    _git_package_tree_digest, _repo_identity, _source_tree_digest,
)
from release_author_io import (  # noqa: E402
    _directory, _exact, _fsync_directory, _load_json, _regular, _sha256,
)
from release_author_wheel import (  # noqa: E402
    _bind_oauth_wheel_source, _validate_wheel_record,
)


def author_release(
    spec_path: Path,
    output_path: Path,
    security_review_path: Path | None = None,
    *,
    review_request: bool = False,
) -> dict[str, Any]:
    spec_path = _regular(str(spec_path), "spec")
    if output_path.resolve() == spec_path.resolve():
        raise ValueError("output must not overwrite the release spec")
    raw_spec = _load_json(spec_path, "release spec")
    image_provenance = "rebuilt"
    if isinstance(raw_spec, dict) and "image_provenance" in raw_spec:
        image_provenance = raw_spec.get("image_provenance")
        if image_provenance not in release_spec.IMAGE_PROVENANCE_VALUES:
            raise ValueError("image_provenance must be 'rebuilt' or 'inherited'")
    inherited_image = image_provenance == "inherited"
    spec = _exact(
        raw_spec,
        SPEC_KEYS_INHERITED if inherited_image
        else SPEC_KEYS_WITH_PROVENANCE if "image_provenance" in raw_spec
        else SPEC_KEYS,
        "release spec",
    )
    if spec.get("schema") != 1:
        raise ValueError("release spec schema must be 1")
    release_id = spec.get("release_id")
    if not isinstance(release_id, str) or not RELEASE_ID_RE.fullmatch(release_id) \
            or len(release_id) > 64:
        raise ValueError(
            "release_id must match menhir-prod-<major>.<minor>.<patch>-<sequence>"
        )
    release_author = spec.get("release_author")
    if not isinstance(release_author, str) or len(release_author) > 128 \
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._@+-]*", release_author):
        raise ValueError("release_author must be a safe bounded identity")
    deployment_class = spec.get("deployment_class")
    if deployment_class not in DEPLOYMENT_CLASSES:
        raise ValueError("deployment_class is invalid")
    ingress_mode = spec.get("ingress_mode")
    if ingress_mode not in INGRESS_MODES:
        raise ValueError("ingress_mode is invalid")
    notes_json_sha256 = spec.get("notes_json_sha256")
    notes_markdown_sha256 = spec.get("notes_markdown_sha256")
    for key, value in (
        ("notes_json_sha256", notes_json_sha256),
        ("notes_markdown_sha256", notes_markdown_sha256),
    ):
        if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
            raise ValueError(f"{key} must be a 64-char lowercase sha256")

    repo_paths = _exact(spec.get("repositories"), REPOSITORIES, "repositories")
    repo_identities = {
        name: _repo_identity(repo_paths[name], name) for name in sorted(REPOSITORIES)
    }
    repos = {name: repo_identities[name][1] for name in sorted(REPOSITORIES)}
    repo_remotes = {name: repo_identities[name][2] for name in sorted(REPOSITORIES)}

    images = _exact(spec.get("images"), IMAGES, "images")
    for name, digest in images.items():
        if not isinstance(digest, str) or not DIGEST_RE.fullmatch(digest):
            raise ValueError(f"images.{name} must be a sha256 digest")
    image_refs = None
    if inherited_image:
        image_refs = {}
    else:
        image_refs = _exact(spec.get("image_refs"), IMAGES, "image_refs")
    for name, reference in image_refs.items():
        if not isinstance(reference, str) \
                or release_spec.IMAGE_REF_RE.fullmatch(reference) is None \
                or not reference.endswith("@" + images[name]):
            raise ValueError(
                f"image_refs.{name} must be an immutable matching reference"
            )

    evidence_values = _exact(
        spec.get("evidence"),
        INHERITED_EVIDENCE if inherited_image else EVIDENCE,
        "evidence",
    )
    evidence = {
        name: (
            _directory(evidence_values[name], f"evidence.{name}")
            if name == "wheelhouse"
            else _regular(evidence_values[name], f"evidence.{name}")
        )
        for name in (INHERITED_EVIDENCE if inherited_image else EVIDENCE)
    }
    oauth_sha = _sha256(evidence["oauth_wheel"])
    oauth_repo, oauth_commit, _ = repo_identities["archolith_oauth"]
    oauth_source_tree_sha = _bind_oauth_wheel_source(
        oauth_repo, oauth_commit, evidence["oauth_wheel"]
    )
    wheel_manifest_sha = _sha256(evidence["wheel_manifest"])
    docker_manifest_sha = _sha256(evidence["dockerfile_wheel_manifest"])
    verify_wheelhouse.verify(
        evidence["wheelhouse"],
        evidence["dockerfile_wheel_manifest"],
        docker_manifest_sha,
        oauth_sha,
    )
    provenance_sha = _validate_provenance(
        evidence["provenance"], repos, repo_remotes, images, oauth_sha,
        wheel_manifest_sha, docker_manifest_sha,
    )
    if inherited_image:
        # No image was built, so there is no new publication to attest. The
        # digests were already proven equal to the prior release authority by
        # release_spec; bind the sbom/scan evidence directly.
        image_publication = {
            "sbom_sha256": _sha256(evidence["sbom"]),
            "scan_evidence_sha256": _sha256(evidence["scan"]),
        }
    else:
        image_publication = release_spec.validate_image_publication(
            publication_path=evidence["image_publication"],
            metadata_path=evidence["image_metadata"],
            identity_path=evidence["image_identity"],
            archive_path=evidence["image_archive"],
            sbom_path=evidence["sbom"],
            scan_path=evidence["scan"],
            publication_attestation_path=evidence["publication_attestation"],
            attestation_trusted_root_path=evidence["attestation_trusted_root"],
            menhir_commit=repos["menhir"],
            menhir_digest=images["menhir"],
            menhir_ref=image_refs["menhir"],
            base_ref=image_refs["base"],
            release_id=release_id,
            wheel_manifest_sha256=docker_manifest_sha,
            oauth_wheel_sha256=oauth_sha,
        )

    rendered_values = _exact(spec.get("rendered"), RENDERED, "rendered")
    rendered_paths = {}
    rendered = {}
    for key in sorted(RENDERED):
        value = rendered_values[key]
        if key in LITERAL_RENDERED_DIGESTS and isinstance(value, str) \
                and value.startswith("sha256:"):
            if not DIGEST_RE.fullmatch(value):
                raise ValueError(f"rendered.{key} literal digest is invalid")
            rendered[key] = value.removeprefix("sha256:")
            continue
        if isinstance(value, str) and value.startswith("sha256:"):
            raise ValueError(f"rendered.{key} does not permit a literal digest")
        rendered_paths[key] = _regular(value, f"rendered.{key}")
    _validate_policy_env_binding(
        rendered_paths["policy_sha256"],
        rendered_paths["production_env_sha256"],
    )
    rendered.update({key: _sha256(path) for key, path in rendered_paths.items()})
    network = spec.get("network")
    if not isinstance(network, dict):
        raise ValueError("network must be an object")

    initial = spec.get("initial_release")
    if not isinstance(initial, bool):
        raise ValueError("initial_release must be boolean")
    prior_value = spec.get("prior_release")
    prior_route_path = _regular(spec.get("prior_route"), "prior_route")
    prior_route_sha = _sha256(prior_route_path)
    initial_host_state = spec.get("initial_host_state")
    if initial:
        if prior_value is not None:
            raise ValueError("initial release must not supply prior_release")
        initial_images = _exact(
            spec.get("initial_prior_images"),
            frozenset({"menhir", "neo4j", "caddy"}),
            "initial_prior_images",
        )
        for name, digest in initial_images.items():
            if not isinstance(digest, str) or not DIGEST_RE.fullmatch(digest):
                raise ValueError(f"initial_prior_images.{name} must be a sha256 digest")
        rollback = {
            "initial_release": True,
            "prior_release_id": "",
            "prior_release_sha256": "",
            "prior_images": initial_images,
            "prior_route_sha256": prior_route_sha,
            "initial_host_state_sha256": _sha256(
                _regular(initial_host_state, "initial_host_state")
            ),
        }
    else:
        if not isinstance(prior_value, str) or not prior_value:
            raise ValueError("non-initial release requires prior_release")
        prior_path = _regular(prior_value, "prior_release")
        prior = menhir_schema.validate_release(str(prior_path))
        if initial_host_state is not None:
            raise ValueError("non-initial release must not supply initial_host_state")
        rollback = {
            "initial_release": False,
            "prior_release_id": prior["release_id"],
            "prior_release_sha256": _sha256(prior_path),
            "prior_images": {
                name: prior["images"][name] for name in ("menhir", "neo4j", "caddy")
            },
            "prior_route_sha256": prior_route_sha,
            "initial_host_state_sha256": "",
        }

    secret_versions = _exact(
        spec.get("secret_version_ids"), SECRET_VERSIONS, "secret_version_ids"
    )
    artifact_sources = spec.get("artifact_sources")
    if not isinstance(artifact_sources, dict) or set(artifact_sources) != REQUIRED_ARTIFACT_DESTINATIONS:
        missing = sorted(REQUIRED_ARTIFACT_DESTINATIONS - set(artifact_sources or {}))
        extra = sorted(set(artifact_sources or {}) - REQUIRED_ARTIFACT_DESTINATIONS)
        raise ValueError(
            "artifact_sources must match installed-artifacts.json: "
            f"missing={missing}, extra={extra}"
        )
    artifacts: dict[str, dict[str, str]] = {}
    for destination, source in sorted(artifact_sources.items()):
        if destination in RENDERED_ARTIFACT_DESTINATIONS:
            rendered_key = RENDERED_ARTIFACT_DESTINATIONS[destination]
            if source != {"kind": "rendered", "rendered_key": rendered_key}:
                raise ValueError(
                    f"artifact_sources[{destination}] must reference rendered {rendered_key}"
                )
            artifacts[destination] = {
                "kind": "rendered",
                "sha256": rendered[rendered_key],
                "rendered_key": rendered_key,
            }
            continue
        if not isinstance(source, dict) or set(source) != {"kind", "repository", "path"} \
                or source.get("kind") != "git" or source.get("repository") not in REPOSITORIES:
            raise ValueError(
                f"artifact_sources[{destination}] must be a committed git blob mapping"
            )
        repository = source["repository"]
        source_path = source.get("path")
        repo_path, commit, _ = repo_identities[repository]
        data, blob_oid = _git_blob(
            repo_path, commit, source_path, f"artifact_sources[{destination}]"
        )
        artifacts[destination] = {
            "kind": "git",
            "sha256": hashlib.sha256(data).hexdigest(),
            "repository": repository,
            "commit": commit,
            "path": source_path,
            "blob_oid": blob_oid,
        }

    release = {
        "schema": 1,
        "release_id": release_id,
        "release_author": release_author,
        "deployment_class": deployment_class,
        "ingress_mode": ingress_mode,
        "notes_json_sha256": notes_json_sha256,
        "notes_markdown_sha256": notes_markdown_sha256,
        "repos": repos,
        "repo_remotes": repo_remotes,
        "oauth_wheel_sha256": oauth_sha,
        "oauth_wheel_source": {
            "repository": "archolith_oauth",
            "commit": oauth_commit,
            "source_tree_sha256": oauth_source_tree_sha,
            "wheel_sha256": oauth_sha,
        },
        "images": images,
        "wheel_manifest_sha256": wheel_manifest_sha,
        "dockerfile_wheel_manifest_sha256": docker_manifest_sha,
        "sbom_sha256": image_publication["sbom_sha256"],
        "scan_evidence_sha256": image_publication["scan_evidence_sha256"],
        "provenance_sha256": provenance_sha,
        "rendered": rendered,
        "network": network,
        "rollback_anchors": rollback,
        "secret_version_ids": secret_versions,
        "artifacts": artifacts,
        "deployment": {
            "topology": "same-host-docker",
            "legacy_container": "menhir-prod-app",
            "production_container": "menhir-prod-app",
            "candidate_container": "menhir-candidate-app",
            "legacy_database_container": "menhir-prod-neo4j",
            "candidate_database_container": "menhir-candidate-neo4j",
            "compose_project": "menhir-prod",
            "compose_service": "menhir",
        },
    }
    if not inherited_image:
        # A rebuilt release records its immutable refs and its publication;
        # unchanged from before inherited releases existed.
        release["image_refs"] = image_refs
        release["image_publication"] = image_publication

    authority_sha = menhir_schema.release_authority_sha256(release)
    if review_request:
        if security_review_path is not None:
            raise ValueError("review request generation does not accept a security review")
        document = {
            "schema": 1,
            "kind": "menhir-production-security-review-request",
            "authority_sha256": authority_sha,
            "release": release,
        }
    else:
        if security_review_path is None:
            raise ValueError("an independent security review is required")
        review_path = _regular(str(security_review_path), "security_review")
        if output_path.resolve() == review_path.resolve():
            raise ValueError("output must not overwrite the security review")
        review = _exact(
            _load_json(review_path, "security review"),
            SECURITY_REVIEW_KEYS,
            "security review",
        )
        if review.get("schema") != 1 \
                or review.get("kind") != "menhir-production-security-review":
            raise ValueError("security review kind/schema is invalid")
        if review.get("authority_sha256") != authority_sha:
            raise ValueError("security review does not bind the exact release authority")
        if review.get("release_author") != release_author:
            raise ValueError("security review release_author mismatch")
        release["security_review"] = {
            **review,
            "review_artifact_sha256": _sha256(review_path),
        }
        document = release

    parent = output_path.resolve().parent
    parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".release.", dir=parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(document, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o400)
        if not review_request:
            menhir_schema.validate_release(temporary)
        os.replace(temporary, output_path)
        _fsync_directory(parent)
    finally:
        if os.path.exists(temporary):
            # Windows refuses unlinking a read-only file. Validation failures
            # must preserve the original error instead of being masked by
            # temporary-file cleanup.
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
            os.unlink(temporary)
    return document


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", required=True, type=Path)
    destination = parser.add_mutually_exclusive_group(required=True)
    destination.add_argument("--output", type=Path)
    destination.add_argument("--review-request", type=Path)
    parser.add_argument("--security-review", type=Path)
    args = parser.parse_args(argv[1:])
    try:
        if args.review_request is not None:
            if args.security_review is not None:
                raise ValueError("--review-request cannot be combined with --security-review")
            author_release(args.spec, args.review_request, review_request=True)
        else:
            if args.security_review is None:
                raise ValueError("--security-review is required with --output")
            author_release(args.spec, args.output, args.security_review)
    except (OSError, subprocess.CalledProcessError, ValueError) as exc:
        print(f"release authoring failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
