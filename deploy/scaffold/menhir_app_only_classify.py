"""Bundle loading and app-only release classification."""

from __future__ import annotations

import copy
import os
import stat
import tempfile
from pathlib import Path
from typing import Any

from menhir_app_only_constants import (
    ALLOWED_ENV_CHANGES, ALLOWED_RELEASE_SCALARS, APPROVAL_NAME, BUNDLE_ID, DIGEST, ID, LIVE_ENV,
    LIVE_RELEASE, SCHEMA, STAGING_RECEIPT_NAME, UPLOAD_ROOT,
)
from menhir_app_only_core import (
    AppOnlyError, atomic_bytes, is_app_only_source_path, json_differences, parse_env,
    require_root_file, require_upload, run, set_path, sha256, strict_load,
)


def classify_release(
    live: dict[str, Any], candidate: dict[str, Any], live_sha: str,
    live_env: dict[str, str], candidate_env: dict[str, str], candidate_env_sha: str,
) -> dict[str, Any]:
    live_id = live.get("release_id")
    candidate_id = candidate.get("release_id")
    if not isinstance(live_id, str) or not isinstance(candidate_id, str) or not ID.fullmatch(candidate_id):
        raise AppOnlyError("release identity is invalid")
    if candidate_id == live_id:
        raise AppOnlyError("changed app image must use a new immutable release_id")
    live_image = live.get("images", {}).get("menhir")
    candidate_image = candidate.get("images", {}).get("menhir")
    if not isinstance(candidate_image, str) or not DIGEST.fullmatch(candidate_image):
        raise AppOnlyError("candidate Menhir image digest is invalid")
    if candidate_image == live_image:
        raise AppOnlyError("app-only release does not change the Menhir image")

    rollback = candidate.get("rollback_anchors")
    expected_rollback = {
        "prior_release_id": live_id,
        "prior_release_sha256": live_sha,
        "prior_images": {
            "menhir": live_image,
            "neo4j": live.get("images", {}).get("neo4j"),
            "caddy": live.get("images", {}).get("caddy"),
        },
    }
    if not isinstance(rollback, dict) or any(
        rollback.get(key) != value for key, value in expected_rollback.items()
    ):
        raise AppOnlyError("candidate rollback anchors do not bind the exact live release")

    if set(live_env) != set(candidate_env):
        raise AppOnlyError("candidate production environment adds or removes keys")
    changed_env = sorted(key for key in live_env if live_env[key] != candidate_env[key])
    if not changed_env or not set(changed_env).issubset(ALLOWED_ENV_CHANGES):
        raise AppOnlyError("protected production environment differs: " + ", ".join(changed_env))
    expected_env = {
        "MENHIR_RELEASE_ID": candidate_id,
        "MENHIR_RELEASE_COMMIT": candidate.get("repos", {}).get("menhir"),
    }
    for key, value in expected_env.items():
        if candidate_env.get(key) != value:
            raise AppOnlyError(f"candidate environment {key} is not release-bound")
    image_ref = candidate_env.get("MENHIR_IMAGE", "")
    if not image_ref.endswith("@" + candidate_image):
        raise AppOnlyError("candidate MENHIR_IMAGE is not bound to the release digest")
    live_image_ref = live_env.get("MENHIR_IMAGE", "")
    if image_ref.rsplit("@", 1)[0] != live_image_ref.rsplit("@", 1)[0]:
        raise AppOnlyError("candidate changes the Menhir image repository")
    if candidate_env.get("NEO4J_IMAGE") != live_env.get("NEO4J_IMAGE"):
        raise AppOnlyError("candidate changes the Neo4j image reference")
    if candidate.get("rendered", {}).get("production_env_sha256") != candidate_env_sha:
        raise AppOnlyError("candidate production.env digest is not release-bound")

    normalized = copy.deepcopy(candidate)
    for path in ALLOWED_RELEASE_SCALARS:
        target: Any = live
        for key in path:
            target = target[key]
        set_path(normalized, path, target)
    normalized["security_review"] = copy.deepcopy(live.get("security_review"))
    normalized["rollback_anchors"] = copy.deepcopy(live.get("rollback_anchors"))

    live_artifacts = live.get("artifacts")
    candidate_artifacts = normalized.get("artifacts")
    if not isinstance(live_artifacts, dict) or not isinstance(candidate_artifacts, dict):
        raise AppOnlyError("release artifact authority is missing")
    env_artifact = "/srv/menhir/production/release/production.env"
    if env_artifact not in candidate_artifacts or env_artifact not in live_artifacts:
        raise AppOnlyError("production.env artifact authority is missing")
    candidate_artifacts[env_artifact] = copy.deepcopy(live_artifacts[env_artifact])
    for path, row in candidate_artifacts.items():
        prior = live_artifacts.get(path)
        if not isinstance(row, dict) or not isinstance(prior, dict):
            continue
        if row.get("repository") == "menhir" and prior.get("repository") == "menhir":
            without_commit = {key: value for key, value in row.items() if key != "commit"}
            prior_without_commit = {key: value for key, value in prior.items() if key != "commit"}
            if without_commit == prior_without_commit and "commit" in row and "commit" in prior:
                row["commit"] = prior["commit"]

    differences = json_differences(live, normalized)
    if differences:
        raise AppOnlyError(
            "protected release surfaces differ: " + ", ".join(differences[:12])
        )
    return {
        "classification": "app-only",
        "live_release_id": live_id,
        "candidate_release_id": candidate_id,
        "prior_image": live_image,
        "candidate_image": candidate_image,
        "changed_environment_keys": changed_env,
    }


def validate_source_manifest(
    manifest: dict[str, Any], release_sha: str, env_sha: str, candidate_id: str,
) -> None:
    if set(manifest) != {"schema", "kind", "release_id", "release_sha256", "files"}:
        raise AppOnlyError("source install-bundle manifest keys mismatch")
    if manifest.get("schema") != 1 or manifest.get("kind") != "menhir-release-install-bundle":
        raise AppOnlyError("source install-bundle manifest schema mismatch")
    if manifest.get("release_id") != candidate_id or manifest.get("release_sha256") != release_sha:
        raise AppOnlyError("source install-bundle release binding mismatch")
    files = manifest.get("files")
    if not isinstance(files, dict):
        raise AppOnlyError("source install-bundle files are missing")
    required = {
        "/srv/menhir/production/release/release.json": release_sha,
        "/srv/menhir/production/release/production.env": env_sha,
    }
    for path, digest in required.items():
        row = files.get(path)
        if not isinstance(row, dict) or row.get("sha256") != digest:
            raise AppOnlyError(f"source install-bundle does not bind {path}")


def recompute_source_classification(
    history_bundle: Path, live_commit: str, candidate_commit: str,
) -> tuple[list[str], list[str]]:
    """Recompute the protected-path decision from commit-addressed Git objects."""
    if history_bundle.stat().st_size > 128 * 1024 * 1024:
        raise AppOnlyError("source history bundle is unexpectedly large")
    with tempfile.TemporaryDirectory(prefix="menhir-app-only-git-") as temporary:
        repository = Path(temporary) / "repository.git"
        run(["git", "clone", "--quiet", "--bare", str(history_bundle), str(repository)], 30)
        observed_live = run(
            ["git", "-C", str(repository), "rev-parse", "refs/heads/live^{commit}"], 10,
        )
        observed_candidate = run(
            ["git", "-C", str(repository), "rev-parse", "refs/heads/candidate^{commit}"], 10,
        )
        if observed_live != live_commit or observed_candidate != candidate_commit:
            raise AppOnlyError("source history bundle is not bound to the release commits")
        output = run([
            "git", "-C", str(repository), "diff", "--name-only",
            "--diff-filter=ACDMRTUXB", observed_live, observed_candidate,
        ], 30)
    changed = sorted(set(output.splitlines()))
    if not changed or any(
        not path or path.startswith("/") or ".." in path.split("/") for path in changed
    ):
        raise AppOnlyError("trusted source diff is empty or invalid")
    forbidden = sorted(path for path in changed if not is_app_only_source_path(path))
    return changed, forbidden


def load_bundle(
    bundle_id: str, destination: Path | None = None, *, require_authority: bool = True,
) -> dict[str, Any]:
    if not BUNDLE_ID.fullmatch(bundle_id):
        raise AppOnlyError("bundle id must be 32 lowercase hexadecimal characters")
    bundle = UPLOAD_ROOT / f"app-{bundle_id}"
    try:
        info = bundle.lstat()
    except OSError as exc:
        raise AppOnlyError("uploaded app-only bundle is missing") from exc
    if bundle.is_symlink() or not stat.S_ISDIR(info.st_mode) or info.st_uid != 1000 \
            or stat.S_IMODE(info.st_mode) & 0o077:
        raise AppOnlyError("uploaded app-only bundle must be a private directory owned by thron")
    base_names = (
        "app-only-manifest.json", "release.json", "production.env",
        "source-manifest.json", "classification-evidence.json", "source-history.bundle",
    )
    names = base_names + ((STAGING_RECEIPT_NAME, APPROVAL_NAME) if require_authority else ())
    for name in names:
        require_upload(bundle / name, name)
    manifest = strict_load(bundle / "app-only-manifest.json")
    if set(manifest) != {"schema", "kind", "source_bundle_sha256", "files"} \
            or manifest.get("schema") != 1 or manifest.get("kind") != "menhir-app-only-bundle":
        raise AppOnlyError("app-only bundle manifest schema mismatch")
    expected_files = set(names) - {"app-only-manifest.json"}
    files = manifest.get("files")
    if not isinstance(files, dict) or set(files) != expected_files:
        raise AppOnlyError("app-only bundle file set mismatch")
    for name in expected_files:
        if files[name] != sha256(bundle / name):
            raise AppOnlyError(f"app-only bundle digest mismatch: {name}")
    if manifest["source_bundle_sha256"] != sha256(bundle / "source-manifest.json"):
        raise AppOnlyError("app-only source manifest digest mismatch")
    if destination is not None:
        destination.mkdir(parents=True, mode=0o700)
        os.chown(destination, 0, 0)
        for name in names:
            atomic_bytes(destination / name, (bundle / name).read_bytes(), 0o400)
        bundle = destination
    release = strict_load(bundle / "release.json")
    env = parse_env(bundle / "production.env")
    source = strict_load(bundle / "source-manifest.json")
    evidence = strict_load(bundle / "classification-evidence.json")
    release_sha = sha256(bundle / "release.json")
    env_sha = sha256(bundle / "production.env")
    validate_source_manifest(source, release_sha, env_sha, str(release.get("release_id", "")))
    if set(evidence) != {
        "schema", "kind", "live_commit", "candidate_commit", "changed_paths",
        "forbidden_matches", "generated_utc",
    } or evidence.get("schema") != 1 or evidence.get("kind") != "menhir-app-only-classification":
        raise AppOnlyError("app-only classification evidence schema mismatch")
    changed_paths = evidence.get("changed_paths")
    if not isinstance(changed_paths, list) or not changed_paths \
            or changed_paths != sorted(set(changed_paths)) \
            or any(not isinstance(path, str) or not path or path.startswith("/") or ".." in path.split("/") for path in changed_paths):
        raise AppOnlyError("app-only changed-path evidence is invalid")
    if evidence.get("forbidden_matches") != []:
        raise AppOnlyError("source diff contains protected app-only paths")
    if evidence.get("live_commit") != strict_load(LIVE_RELEASE).get("repos", {}).get("menhir") \
            or evidence.get("candidate_commit") != release.get("repos", {}).get("menhir"):
        raise AppOnlyError("classification evidence is not bound to the live and candidate commits")
    trusted_changed, trusted_forbidden = recompute_source_classification(
        bundle / "source-history.bundle", evidence["live_commit"], evidence["candidate_commit"],
    )
    if changed_paths != trusted_changed or evidence.get("forbidden_matches") != trusted_forbidden:
        raise AppOnlyError("caller classification differs from the trusted source diff")
    if trusted_forbidden:
        raise AppOnlyError("source diff contains protected app-only paths")
    return {
        "path": bundle, "release": release, "env": env,
        "release_sha": release_sha, "env_sha": env_sha, "evidence": evidence,
    }


def validate_release_file(path: Path) -> None:
    require_root_file(SCHEMA, "release schema validator")
    run(["python3", str(SCHEMA), "validate-release", str(path)], 30)


def classify_bundle(
    bundle_id: str, destination: Path | None = None, *, require_authority: bool = True,
) -> tuple[dict[str, Any], dict[str, Any]]:
    require_root_file(LIVE_RELEASE, "live release")
    require_root_file(LIVE_ENV, "live production environment")
    bundle = load_bundle(bundle_id, destination, require_authority=require_authority)
    validate_release_file(bundle["path"] / "release.json")
    live = strict_load(LIVE_RELEASE)
    result = classify_release(
        live, bundle["release"], sha256(LIVE_RELEASE), parse_env(LIVE_ENV),
        bundle["env"], bundle["env_sha"],
    )
    result["candidate_release_sha256"] = bundle["release_sha"]
    return bundle, result


def live_info() -> dict[str, Any]:
    require_root_file(LIVE_RELEASE, "live release")
    value = strict_load(LIVE_RELEASE)
    return {
        "release_id": value.get("release_id"),
        "menhir_commit": value.get("repos", {}).get("menhir"),
        "menhir_image": value.get("images", {}).get("menhir"),
        "release_sha256": sha256(LIVE_RELEASE),
    }
