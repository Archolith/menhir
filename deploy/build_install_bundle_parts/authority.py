"""Repository and release-spec authority validation for the bundle."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

_PARTS_PARENT = Path(__file__).resolve().parents[1]
for _candidate in (str(_PARTS_PARENT), str(_PARTS_PARENT / "lib")):
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import menhir_schema  # noqa: E402
import release_spec  # noqa: E402

from .core import (  # noqa: E402
    ALLOWED_GIT_MODES as ALLOWED_GIT_MODES,
    EVIDENCE_DIGESTS as EVIDENCE_DIGESTS,
    OID_RE as OID_RE,
    PUBLICATION_EVIDENCE as PUBLICATION_EVIDENCE,
    REPOSITORIES as REPOSITORIES,
    SPEC_KEYS as SPEC_KEYS,
    SPEC_KEYS_INHERITED as SPEC_KEYS_INHERITED,
    SPEC_KEYS_WITH_PROVENANCE as SPEC_KEYS_WITH_PROVENANCE,
)
from .fsio import (  # noqa: E402
    _canonical_source_path as _canonical_source_path,
    _directory as _directory,
    _exact_keys as _exact_keys,
    _regular_file as _regular_file,
    _sha256_file as _sha256_file,
)


def _run_git(repo: Path, arguments: Sequence[str], label: str) -> bytes:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *arguments],
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError(f"git lookup failed for {label}") from exc
    return result.stdout


def _validate_repository(
    path_value: Any, name: str, expected_remote: str, commit: str
) -> Path:
    repo = _directory(path_value, f"repository {name}")
    top = _run_git(repo, ["rev-parse", "--show-toplevel"], name).decode(
        "utf-8", errors="strict"
    ).strip()
    if Path(top).resolve(strict=True) != repo.resolve(strict=True):
        raise ValueError(f"repository {name} path is not its canonical worktree root")
    remote = _run_git(repo, ["remote", "get-url", "origin"], name).decode(
        "utf-8", errors="strict"
    ).strip()
    match = re.fullmatch(
        r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
        r"([^/]+)/([^/]+?)(?:\.git)?/?",
        remote,
    )
    canonical_remote = (
        f"https://github.com/{match.group(1)}/{match.group(2)}.git"
        if match else ""
    )
    if canonical_remote != expected_remote:
        raise ValueError(f"repository {name} origin is inconsistent with release authority")
    object_type = _run_git(repo, ["cat-file", "-t", f"{commit}^{{commit}}"], name)
    if object_type != b"commit\n":
        raise ValueError(f"repository {name} release-bound commit is missing")
    return repo


def _git_blob(
    repo: Path, commit: str, source_path: str, expected_oid: str, label: str
) -> bytes:
    source_path = _canonical_source_path(source_path, label)
    raw = _run_git(
        repo,
        ["ls-tree", "-z", commit, "--", f":(literal){source_path}"],
        label,
    )
    records = [record for record in raw.split(b"\0") if record]
    if len(records) != 1:
        raise ValueError(f"{label} is missing or is not one exact committed object")
    try:
        metadata, encoded_path = records[0].split(b"\t", 1)
        mode, object_type, oid = metadata.decode("ascii").split(" ")
        observed_path = encoded_path.decode("utf-8", errors="strict")
    except (ValueError, UnicodeError) as exc:
        raise ValueError(f"{label} has an invalid git tree record") from exc
    if observed_path != source_path:
        raise ValueError(f"{label} git tree path is inconsistent")
    if mode not in ALLOWED_GIT_MODES or object_type != "blob":
        raise ValueError(
            f"{label} has unsafe or unknown git mode/type: {mode} {object_type}"
        )
    if not OID_RE.fullmatch(oid) or oid != expected_oid:
        raise ValueError(f"{label} blob object id differs from release authority")
    return _run_git(repo, ["cat-file", "blob", oid], label)


def _validate_spec_relationship(
    release: dict[str, Any], spec: dict[str, Any]
) -> tuple[dict[str, Path], dict[str, Path]]:
    image_provenance = spec.get("image_provenance", "rebuilt")         if isinstance(spec, dict) else "rebuilt"
    if image_provenance not in release_spec.IMAGE_PROVENANCE_VALUES:
        raise ValueError("release spec image_provenance is invalid")
    inherited_image = image_provenance == "inherited"
    _exact_keys(
        spec,
        SPEC_KEYS_INHERITED if inherited_image
        else SPEC_KEYS_WITH_PROVENANCE if "image_provenance" in spec
        else SPEC_KEYS,
        "release spec",
    )
    if inherited_image:
        # The authority this release inherits its image binding from must be
        # the release the spec was proven against, and the record itself must
        # be in the pre-publication shape (no image_refs / image_publication).
        inherited = spec.get("inherited_image_release")
        if not isinstance(inherited, dict)                 or set(inherited) != {"release_id", "release_sha256"}:
            raise ValueError("release spec inherited_image_release is malformed")
        if "image_refs" in release or "image_publication" in release:
            raise ValueError(
                "inherited-image release authority must not carry image_refs "
                "or image_publication"
            )
    if spec.get("schema") != 1:
        raise ValueError("release spec schema must be 1")
    for key in (
        "release_id", "release_author", "deployment_class",
        "notes_json_sha256", "notes_markdown_sha256", "ingress_mode",
        "images", "network", "secret_version_ids",
    ):
        if spec.get(key) != release.get(key):
            raise ValueError(f"release spec {key} differs from release authority")
    image_refs = {}
    if not inherited_image:
        image_refs = _exact_keys(
            spec.get("image_refs"), set(release["images"]),
            "release spec image_refs",
        )
    for name, digest in release["images"].items():
        if inherited_image:
            break
        reference = image_refs[name]
        if not isinstance(reference, str) \
                or release_spec.IMAGE_REF_RE.fullmatch(reference) is None \
                or not reference.endswith("@" + digest):
            raise ValueError(
                f"release spec image_refs.{name} differs from image authority"
            )
    repositories = _exact_keys(
        spec.get("repositories"), REPOSITORIES, "release spec repositories"
    )
    repos = _exact_keys(release.get("repos"), REPOSITORIES, "release repositories")
    remotes = _exact_keys(
        release.get("repo_remotes"), REPOSITORIES, "release repository remotes"
    )
    repo_paths = {
        name: _validate_repository(
            repositories[name], name, remotes[name], repos[name]
        )
        for name in sorted(REPOSITORIES)
    }

    rendered_values = spec.get("rendered")
    if not isinstance(rendered_values, dict) or set(rendered_values) != set(release["rendered"]):
        raise ValueError("release spec rendered keys differ from release authority")
    rendered_paths: dict[str, Path] = {}
    for key, expected_digest in sorted(release["rendered"].items()):
        value = rendered_values[key]
        if isinstance(value, str) and value.startswith("sha256:"):
            if value.removeprefix("sha256:") != expected_digest:
                raise ValueError(f"release spec rendered.{key} differs from release authority")
            continue
        path = _regular_file(value, f"release spec rendered.{key}")
        if _sha256_file(path) != expected_digest:
            raise ValueError(f"release spec rendered.{key} digest drift")
        rendered_paths[key] = path

    evidence_keys = (
        frozenset(EVIDENCE_DIGESTS)
        | frozenset({"wheelhouse"})
        | (frozenset() if inherited_image else PUBLICATION_EVIDENCE)
    )
    evidence = _exact_keys(
        spec.get("evidence"), evidence_keys, "release spec evidence"
    )
    _directory(evidence["wheelhouse"], "release spec evidence.wheelhouse")
    for key, release_key in EVIDENCE_DIGESTS.items():
        path = _regular_file(evidence.get(key), f"release spec evidence.{key}")
        if _sha256_file(path) != release[release_key]:
            raise ValueError(f"release spec evidence.{key} digest drift")
    publication = None if inherited_image else release_spec.validate_image_publication(
        publication_path=_regular_file(
            evidence["image_publication"],
            "release spec evidence.image_publication",
        ),
        metadata_path=_regular_file(
            evidence["image_metadata"], "release spec evidence.image_metadata",
        ),
        identity_path=_regular_file(
            evidence["image_identity"], "release spec evidence.image_identity",
        ),
        archive_path=_regular_file(
            evidence["image_archive"], "release spec evidence.image_archive",
        ),
        sbom_path=_regular_file(evidence["sbom"], "release spec evidence.sbom"),
        scan_path=_regular_file(evidence["scan"], "release spec evidence.scan"),
        publication_attestation_path=_regular_file(
            evidence["publication_attestation"],
            "release spec evidence.publication_attestation",
        ),
        attestation_trusted_root_path=_regular_file(
            evidence["attestation_trusted_root"],
            "release spec evidence.attestation_trusted_root",
        ),
        menhir_commit=release["repos"]["menhir"],
        menhir_digest=release["images"]["menhir"],
        menhir_ref=image_refs["menhir"],
        base_ref=image_refs["base"],
        release_id=release["release_id"],
        wheel_manifest_sha256=release["dockerfile_wheel_manifest_sha256"],
        oauth_wheel_sha256=release["oauth_wheel_sha256"],
    )
    if not inherited_image and publication != release.get("image_publication"):
        raise ValueError(
            "release spec image publication differs from release authority"
        )

    initial = spec.get("initial_release")
    if initial is not release["rollback_anchors"]["initial_release"]:
        raise ValueError("release spec initial_release differs from release authority")
    prior_route = _regular_file(spec.get("prior_route"), "release spec prior_route")
    if _sha256_file(prior_route) != release["rollback_anchors"]["prior_route_sha256"]:
        raise ValueError("release spec prior_route digest drift")
    if initial:
        if spec.get("initial_prior_images") \
                != release["rollback_anchors"]["prior_images"]:
            raise ValueError("release spec prior images differ from release authority")
        initial_host = _regular_file(
            spec.get("initial_host_state"), "release spec initial_host_state"
        )
        if _sha256_file(initial_host) \
                != release["rollback_anchors"]["initial_host_state_sha256"]:
            raise ValueError("release spec initial_host_state digest drift")
        if spec.get("prior_release") is not None:
            raise ValueError("initial release spec must not provide prior_release")
    else:
        if spec.get("initial_host_state") is not None:
            raise ValueError("non-initial release spec must not provide initial_host_state")
        prior = _regular_file(spec.get("prior_release"), "release spec prior_release")
        if _sha256_file(prior) != release["rollback_anchors"]["prior_release_sha256"]:
            raise ValueError("release spec prior_release digest drift")
        prior_release = menhir_schema.validate_release(str(prior))
        rollback = release["rollback_anchors"]
        if rollback["prior_release_id"] != prior_release["release_id"] \
                or rollback["prior_images"] != {
                    name: prior_release["images"][name]
                    for name in ("menhir", "neo4j", "caddy")
                }:
            raise ValueError("release spec prior_release authority mismatch")
    return repo_paths, rendered_paths
