"""Release binding: publication verification and release-workspace authority."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .constants import (
    BUNDLE_NAME,
    DEFAULT_PROMOTION_WRAPPER,
    DEPLOYMENT_CLASSES,
    IMAGE_RE,
    OPERATOR_ROOT,
    RELEASE_ID_RE,
    RELEASE_NAME,
    RELEASE_STATE_NAME,
    SCRIPT_DIR,
    SHA256_RE,
)
from .fsio import (
    PersonalDeployError,
    _composite_sha256,
    _directory,
    _load_json,
    _regular_file,
    _sha256,
    _tree_sha256,
)


def _verify_publication(release_state: dict[str, Any]) -> None:
    if release_state.get("phase") != "published":
        raise PersonalDeployError("product release must be published before personal deployment")
    release_id = release_state.get("release_id")
    fragments_value = release_state.get("fragments")
    fragments_dir_value = release_state.get("fragments_dir")
    receipt_digest = release_state.get("publication_receipt_sha256")
    if not isinstance(fragments_dir_value, str) or not Path(fragments_dir_value).is_absolute() \
            or not isinstance(fragments_value, list) or not fragments_value \
            or not isinstance(receipt_digest, str) or SHA256_RE.fullmatch(receipt_digest) is None:
        raise PersonalDeployError("published release has invalid fragment authority")
    fragments_dir = Path(fragments_dir_value)
    archive = fragments_dir.parent / "releases" / str(release_id)
    archive = _directory(archive, "published release archive")
    expected_names = {"publication-receipt.json"}
    bindings: list[dict[str, str]] = []
    for row in fragments_value:
        if not isinstance(row, dict) or set(row) != {"name", "sha256"}:
            raise PersonalDeployError("published release fragment binding is invalid")
        name = row.get("name")
        digest = row.get("sha256")
        if not isinstance(name, str) or Path(name).name != name or not name.endswith(".json") \
                or not isinstance(digest, str) or SHA256_RE.fullmatch(digest) is None \
                or name in expected_names:
            raise PersonalDeployError("published release fragment binding is unsafe")
        expected_names.add(name)
        bindings.append({"name": name, "sha256": digest})
    if bindings != sorted(bindings, key=lambda row: row["name"]):
        raise PersonalDeployError("published release fragments are not sorted")
    if {path.name for path in archive.iterdir()} != expected_names:
        raise PersonalDeployError("published release archive contents are invalid")
    for row in bindings:
        source = fragments_dir / row["name"]
        if source.exists() or source.is_symlink():
            raise PersonalDeployError("published release fragment remains unreleased")
        if _sha256(_regular_file(archive / row["name"], "published release fragment")) \
                != row["sha256"]:
            raise PersonalDeployError("published release fragment digest mismatch")
    receipt_path = _regular_file(
        archive / "publication-receipt.json", "publication receipt"
    )
    if _sha256(receipt_path) != receipt_digest:
        raise PersonalDeployError("publication receipt changed")
    receipt = _load_json(receipt_path, "publication receipt")
    if receipt.get("kind") != "menhir-release-publication" \
            or receipt.get("release_id") != release_id \
            or receipt.get("fragments") != bindings:
        raise PersonalDeployError("publication receipt is not bound to the release")


def _operator_wrapper(deployment_class: str) -> Path:
    if deployment_class == "security-config":
        return SCRIPT_DIR / "personal_security_config.ps1"
    name = (
        "deploy-menhir-app-only.ps1"
        if deployment_class == "app-only"
        else "deploy-menhir.ps1"
    )
    return OPERATOR_ROOT / name


def _root_runner_sha256(release: dict[str, Any], deployment_class: str) -> str:
    if deployment_class == "app-only":
        return _sha256(_regular_file(
            SCRIPT_DIR / "scaffold" / "menhir_app_only.py", "root deployment runner"
        ))
    if deployment_class == "security-config":
        return _composite_sha256((
            ("menhir_app_only.py", _regular_file(
                SCRIPT_DIR / "scaffold" / "menhir_app_only.py", "root deployment dependency"
            )),
            ("menhir_security_config.py", _regular_file(
                SCRIPT_DIR / "scaffold" / "menhir_security_config.py", "root deployment runner"
            )),
        ))
    artifacts = release.get("artifacts")
    row = artifacts.get("/srv/menhir/production/bin/release-run.sh") \
        if isinstance(artifacts, dict) else None
    digest = row.get("sha256") if isinstance(row, dict) else None
    if not isinstance(digest, str) or SHA256_RE.fullmatch(digest) is None:
        raise PersonalDeployError("maintenance root runner is not release-bound")
    return digest


def _release_binding(release_workspace: Path) -> dict[str, Any]:
    release_workspace = _directory(release_workspace, "product release workspace")
    release_state = _load_json(
        release_workspace / RELEASE_STATE_NAME,
        "product release state",
    )
    if release_state.get("kind") != "menhir-release-flow":
        raise PersonalDeployError("product release state is invalid")
    _verify_publication(release_state)
    release_id = release_state.get("release_id")
    release_sha = release_state.get("release_sha256")
    bundle_sha = release_state.get("bundle_sha256")
    deployment_class = release_state.get("deployment_class")
    ingress_mode = release_state.get("ingress_mode")
    if not isinstance(release_id, str) or RELEASE_ID_RE.fullmatch(release_id) is None:
        raise PersonalDeployError("product release ID is invalid")
    if not isinstance(release_sha, str) or SHA256_RE.fullmatch(release_sha) is None \
            or not isinstance(bundle_sha, str) or SHA256_RE.fullmatch(bundle_sha) is None:
        raise PersonalDeployError("product release digests are invalid")
    if deployment_class not in DEPLOYMENT_CLASSES:
        raise PersonalDeployError("product deployment class is invalid")
    if ingress_mode != "cloudflared":
        raise PersonalDeployError("product ingress mode is invalid")
    release_path = _regular_file(release_workspace / RELEASE_NAME, "release authority")
    bundle = _directory(release_workspace / BUNDLE_NAME, "install bundle")
    if _sha256(release_path) != release_sha or _tree_sha256(bundle) != bundle_sha:
        raise PersonalDeployError("finalized product release artifacts changed")
    release = _load_json(release_path, "release authority")
    if release.get("release_id") != release_id:
        raise PersonalDeployError("release authority identity mismatch")
    if release.get("deployment_class") != deployment_class:
        raise PersonalDeployError("product state/release deployment class mismatch")
    if release.get("ingress_mode") != ingress_mode:
        raise PersonalDeployError("product state/release ingress mode mismatch")
    promotion_wrapper = _regular_file(
        DEFAULT_PROMOTION_WRAPPER, "production promotion wrapper"
    )
    operator_wrapper = _regular_file(
        _operator_wrapper(str(deployment_class)), "production operator wrapper"
    )
    for state_key, release_key, filename in (
        ("notes_json_sha256", "notes_json_sha256", "release-notes.json"),
        ("notes_markdown_sha256", "notes_markdown_sha256", "release-notes.md"),
    ):
        state_digest = release_state.get(state_key)
        release_digest = release.get(release_key)
        if not isinstance(state_digest, str) or SHA256_RE.fullmatch(state_digest) is None \
                or release_digest != state_digest:
            raise PersonalDeployError(f"product {filename} authority mismatch")
        if _sha256(_regular_file(release_workspace / filename, filename)) != state_digest:
            raise PersonalDeployError(f"finalized product {filename} changed")
    images = release.get("images")
    if not isinstance(images, dict):
        raise PersonalDeployError("release image authority is invalid")
    for name in ("menhir", "neo4j"):
        if not isinstance(images.get(name), str) or IMAGE_RE.fullmatch(images[name]) is None:
            raise PersonalDeployError(f"release {name} image digest is invalid")
    return {
        "release_workspace": str(release_workspace),
        "release_id": release_id,
        "release_sha256": release_sha,
        "bundle_sha256": bundle_sha,
        "deployment_class": deployment_class,
        "ingress_mode": ingress_mode,
        "menhir_image": images["menhir"],
        "neo4j_image": images["neo4j"],
        "promotion_wrapper_sha256": _sha256(promotion_wrapper),
        "operator_wrapper_sha256": _sha256(operator_wrapper),
        "root_runner_sha256": _root_runner_sha256(release, str(deployment_class)),
    }
