"""Live-versus-candidate release classification for security-config promotions."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import menhir_app_only as app

from menhir_security_config_bundle import load_bundle
from menhir_security_config_constants import (
    ALLOWED_CONFIG_DESTINATIONS, ALLOWED_ENV_CHANGES, ALLOWED_RENDERED_CHANGES, DESTINATIONS, Error,
)


def comparable_artifact(row: Any) -> Any:
    if not isinstance(row, dict):
        return row
    result = copy.deepcopy(row)
    if result.get("kind") == "git":
        result.pop("commit", None)
    return result


def classify_release(
    live: dict[str, Any], candidate: dict[str, Any], live_sha: str,
    live_env: dict[str, str], candidate_env: dict[str, str], bundle: Path,
) -> dict[str, Any]:
    candidate_id = candidate.get("release_id")
    live_id = live.get("release_id")
    if not isinstance(candidate_id, str) or not app.ID.fullmatch(candidate_id) \
            or candidate_id == live_id:
        raise Error("security-config release identity is invalid or not new")
    if candidate.get("deployment_class") != "security-config" \
            or candidate.get("ingress_mode") != "cloudflared":
        raise Error("candidate is not Cloudflared security-config authority")
    for key in ("schema", "network", "deployment", "repo_remotes", "ingress_mode"):
        if candidate.get(key) != live.get(key):
            raise Error(f"protected release field differs: {key}")
    for name in ("yawn_deploy",):
        if candidate.get("repos", {}).get(name) != live.get("repos", {}).get(name):
            raise Error(f"security-config cannot change repository: {name}")
    for name in ("neo4j", "caddy", "base"):
        if candidate.get("images", {}).get(name) != live.get("images", {}).get(name):
            raise Error(f"security-config cannot change image: {name}")
    candidate_image = candidate.get("images", {}).get("menhir")
    if not isinstance(candidate_image, str) or not app.DIGEST.fullmatch(candidate_image):
        raise Error("candidate Menhir image digest is invalid")
    expected_rollback = {
        "prior_release_id": live_id,
        "prior_release_sha256": live_sha,
        "prior_images": {
            "menhir": live.get("images", {}).get("menhir"),
            "neo4j": live.get("images", {}).get("neo4j"),
            "caddy": live.get("images", {}).get("caddy"),
        },
    }
    rollback = candidate.get("rollback_anchors")
    if not isinstance(rollback, dict) or any(rollback.get(k) != v for k, v in expected_rollback.items()):
        raise Error("candidate rollback anchors do not bind the exact live release")
    if set(live_env) != set(candidate_env):
        raise Error("candidate production environment adds or removes keys")
    changed_env = sorted(key for key in live_env if live_env[key] != candidate_env[key])
    if not changed_env or not set(changed_env).issubset(ALLOWED_ENV_CHANGES):
        raise Error("protected production environment differs: " + ", ".join(changed_env))
    expected_env = {
        "MENHIR_RELEASE_ID": candidate_id,
        "MENHIR_RELEASE_COMMIT": candidate.get("repos", {}).get("menhir"),
        "MENHIR_CLIENT_POLICY_DIGEST": app.strict_load(
            bundle / "client-policy.json"
        ).get("canonical_digest"),
    }
    for key, value in expected_env.items():
        if candidate_env.get(key) != value:
            raise Error(f"candidate environment {key} is not release-bound")
    image_ref = candidate_env.get("MENHIR_IMAGE", "")
    live_image_ref = live_env.get("MENHIR_IMAGE", "")
    if not image_ref.endswith("@" + candidate_image) \
            or image_ref.rsplit("@", 1)[0] != live_image_ref.rsplit("@", 1)[0]:
        raise Error("candidate Menhir image reference is invalid")
    if candidate_env.get("NEO4J_IMAGE") != live_env.get("NEO4J_IMAGE"):
        raise Error("candidate changes the Neo4j image reference")
    for key in set(live.get("rendered", {})) | set(candidate.get("rendered", {})):
        if key not in ALLOWED_RENDERED_CHANGES \
                and candidate.get("rendered", {}).get(key) != live.get("rendered", {}).get(key):
            raise Error(f"protected rendered authority differs: {key}")
    live_secrets = live.get("secret_version_ids", {})
    candidate_secrets = candidate.get("secret_version_ids", {})
    for key in set(live_secrets) | set(candidate_secrets):
        if key != "client-policy" and candidate_secrets.get(key) != live_secrets.get(key):
            raise Error(f"security-config cannot rotate secret: {key}")
    policy_digest = app.strict_load(bundle / "client-policy.json").get("canonical_digest")
    if candidate_secrets.get("client-policy") != "sha256-" + str(policy_digest):
        raise Error("client-policy secret version is not bound to its canonical digest")
    live_artifacts = live.get("artifacts")
    candidate_artifacts = candidate.get("artifacts")
    if not isinstance(live_artifacts, dict) or not isinstance(candidate_artifacts, dict) \
            or set(live_artifacts) != set(candidate_artifacts):
        raise Error("release artifact authority differs")
    for destination, row in candidate_artifacts.items():
        prior = live_artifacts[destination]
        if destination in ALLOWED_CONFIG_DESTINATIONS:
            name = next(name for name, value in DESTINATIONS.items() if value == destination)
            if not isinstance(row, dict) or row.get("sha256") != app.sha256(bundle / name):
                raise Error(f"candidate artifact is not bound: {destination}")
        elif comparable_artifact(row) != comparable_artifact(prior):
            raise Error(f"protected artifact differs: {destination}")
    return {
        "classification": "security-config",
        "live_release_id": live_id,
        "candidate_release_id": candidate_id,
        "prior_image": live.get("images", {}).get("menhir"),
        "candidate_image": candidate_image,
        "changed_environment_keys": changed_env,
    }


def classify_bundle(
    bundle_id: str, destination: Path | None = None, *, require_authority: bool = True,
) -> tuple[dict[str, Any], dict[str, Any]]:
    for path, label in ((app.LIVE_RELEASE, "live release"), (app.LIVE_ENV, "live environment")):
        app.require_root_file(path, label)
    bundle = load_bundle(bundle_id, destination, require_authority=require_authority)
    app.validate_release_file(bundle["path"] / "release.json")
    result = classify_release(
        app.strict_load(app.LIVE_RELEASE), bundle["release"], app.sha256(app.LIVE_RELEASE),
        app.parse_env(app.LIVE_ENV), bundle["env"], bundle["path"],
    )
    result["candidate_release_sha256"] = bundle["release_sha"]
    return bundle, result
