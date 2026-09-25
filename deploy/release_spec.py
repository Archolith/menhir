#!/usr/bin/env python3
"""Prepare strict, reproducible inputs for release-author.py."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess  # retained: tests patch MODULE.subprocess
import sys
import tempfile  # retained: tests patch MODULE.tempfile
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

import artifact_authority  # noqa: E402
import menhir_schema  # noqa: E402
import build_release_image  # noqa: E402  # retained: tests read MODULE.build_release_image

from release_spec_errors import ReleaseSpecError  # noqa: E402
from release_spec_constants import (  # noqa: E402
    AUTHOR_RE, DIGEST_RE, ENV_KEY_RE, EVIDENCE_KEYS, IMAGES, IMAGE_KEYS,
    IMAGE_PROVENANCE_VALUES, IMAGE_REF_RE, INHERITED_EVIDENCE_KEYS,
    INPUT_KEYS, INPUT_KEYS_WITH_PROVENANCE, PLACEHOLDER_RE, RELEASE_ID_RE,
    REPOSITORIES, SECRET_KEY_RE, SECRET_VALUE_RE, SECRET_VERSIONS,
    SHA256_HEX_RE, SHA256_RE, VERSION_RE,
)
from release_spec_image import (  # noqa: E402
    CANONICAL_GITHUB_REPOSITORY, IMAGE_ARTIFACT_BINDING_KEYS,
    IMAGE_EVIDENCE_DIGEST_KEYS, IMAGE_EVIDENCE_ENTRY_KEYS, IMAGE_EVIDENCE_KEYS,
    IMAGE_IDENTITY_KEYS, IMAGE_LABEL_KEYS, IMAGE_METADATA_KEYS,
    IMAGE_SUBJECT_KEYS, PUBLICATION_KEYS,
    REVIEWED_GITHUB_ATTESTATION_TRUSTED_ROOT_SHA256,
    _require_sha256, _verify_github_attestation, validate_image_publication,
)
from release_spec_io import (  # noqa: E402
    _absolute, _capture_bytes, _directory, _exact, _load_json, _load_json_bytes,
    _regular, _sha256, _sha256_bytes, _unique_pairs,
)
from release_spec_git import (  # noqa: E402
    _canonical_remote, _git_blob, _git_run, _repo_identity,
)
from release_spec_policy import (  # noqa: E402
    _canonical_json_digest, _reject_secret_material, _render_env,
    _validate_output, _validate_policy, _wheelhouse,
)


# The sole destination-to-source authority for the installed artifact census
# is deploy/artifact-authority.json (see deploy/lib/artifact_authority.py);
# installed-artifacts.json, release-install.sh and verify-artifacts are
# rendered from the same file, so they cannot disagree with this map.
ARTIFACT_SOURCES: dict[str, dict[str, str]] = artifact_authority.sources()


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
    # ARTIFACT_SOURCES was loaded from this checkout's authority file. The
    # census above proves the destination set matches the commit; this proves
    # the destination-to-source map does too, so a dirty authority cannot bind
    # a destination to the wrong committed blob.
    try:
        committed_authority = artifact_authority.sources(json.loads(
            _git_blob(
                menhir_repo, commit, "deploy/artifact-authority.json",
                "artifact authority",
            ).decode("utf-8"),
            object_pairs_hook=_unique_pairs,
        ))
    except (UnicodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ReleaseSpecError("committed artifact authority is invalid") from exc
    if committed_authority != ARTIFACT_SOURCES:
        raise ReleaseSpecError("artifact authority differs from the menhir commit")


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
        encoding="ascii",
        newline="\n",
    )


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
        for key, path in generated.items():
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
