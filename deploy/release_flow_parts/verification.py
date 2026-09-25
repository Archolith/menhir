"""Release-flow state loading and staged-artifact verification."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .core import (
    BUNDLE_NAME,
    CLASS_ORDER,
    KIND,
    LEGACY_STATE_KEYS,
    NOTES_JSON_NAME,
    NOTES_MARKDOWN_NAME,
    PHASES,
    PRE_INGRESS_STATE_KEYS,
    PUBLICATION_NONCE_RE,
    RELEASE_AUTHORITY_BINDINGS,
    RELEASE_ID_RE,
    RELEASE_NAME,
    REVIEW_REQUEST_NAME,
    SCHEMA,
    SHA256_RE,
    SPEC_NAME,
    STATE_KEYS,
    STATE_NAME,
    ReleaseFlowError,
)
from .fragments import _validate_fragment_bindings
from .fsio import _load_json, _regular_file, _sha256, _tree_sha256
from .publication import _verify_publication


def _state_path(workspace: Path) -> Path:
    return workspace / STATE_NAME


def _load_state(workspace: Path) -> dict[str, Any]:
    state = _load_json(_state_path(workspace), "release flow state")
    keys = set(state)
    if keys not in {STATE_KEYS, PRE_INGRESS_STATE_KEYS, LEGACY_STATE_KEYS} \
            or state.get("schema") != SCHEMA or state.get("kind") != KIND:
        raise ReleaseFlowError("release flow state schema is invalid")
    if state.get("phase") not in PHASES:
        raise ReleaseFlowError("release flow phase is invalid")
    if state.get("workspace") != str(workspace):
        raise ReleaseFlowError("release flow state is bound to another workspace")
    if state.get("deployment_class") not in CLASS_ORDER:
        raise ReleaseFlowError("release flow deployment class is invalid")
    if keys == STATE_KEYS and state.get("ingress_mode") != "cloudflared":
        raise ReleaseFlowError("release flow ingress mode is invalid")
    if not isinstance(state.get("release_id"), str) \
            or not RELEASE_ID_RE.fullmatch(state["release_id"]):
        raise ReleaseFlowError("release flow release_id is invalid")
    for key in keys:
        if key.endswith("_sha256"):
            value = state.get(key)
            if value is not None and (not isinstance(value, str) or not SHA256_RE.fullmatch(value)):
                raise ReleaseFlowError(f"release flow digest is invalid: {key}")
    if keys == LEGACY_STATE_KEYS:
        if state["phase"] == "published":
            raise ReleaseFlowError("published release flow lacks publication bindings")
        return state
    fragments_dir = state.get("fragments_dir")
    if not isinstance(fragments_dir, str) or not Path(fragments_dir).is_absolute():
        raise ReleaseFlowError("release flow fragments directory is invalid")
    _validate_fragment_bindings(state.get("fragments"))
    publication_nonce = state.get("publication_nonce")
    receipt_digest = state.get("publication_receipt_sha256")
    if state["phase"] in {"publishing", "published"}:
        if not isinstance(publication_nonce, str) \
                or not PUBLICATION_NONCE_RE.fullmatch(publication_nonce):
            raise ReleaseFlowError("publication transaction nonce is invalid")
        if receipt_digest is None:
            raise ReleaseFlowError("publication transaction has no receipt digest")
    elif publication_nonce is not None or receipt_digest is not None:
        raise ReleaseFlowError("inactive release flow has publication transaction state")
    return state


def _verify_staged_files(workspace: Path, state: dict[str, Any]) -> None:
    bindings = {
        "spec_sha256": workspace / SPEC_NAME,
        "notes_json_sha256": workspace / NOTES_JSON_NAME,
        "notes_markdown_sha256": workspace / NOTES_MARKDOWN_NAME,
        "review_request_sha256": workspace / REVIEW_REQUEST_NAME,
    }
    if state["phase"] in {"bundled", "publishing", "published", "deployed"}:
        bindings.update({
            "security_review_sha256": workspace / "security-review.json",
            "release_sha256": workspace / RELEASE_NAME,
            "bundle_manifest_sha256": workspace / BUNDLE_NAME / "bundle-manifest.json",
        })
    for key, path in bindings.items():
        expected = state.get(key)
        if expected is None or _sha256(_regular_file(path, key)) != expected:
            raise ReleaseFlowError(f"staged release artifact changed: {path.name}")
    if state["phase"] in {"bundled", "publishing", "published", "deployed"}:
        if _tree_sha256(workspace / BUNDLE_NAME) != state.get("bundle_sha256"):
            raise ReleaseFlowError("staged install bundle changed")
    if set(state) == STATE_KEYS:
        _verify_release_authority_bindings(workspace, state)
    if state["phase"] == "published":
        _verify_publication(state)


def _verify_release_authority_bindings(
    workspace: Path,
    state: dict[str, Any],
) -> None:
    expected = {key: state[key] for key in RELEASE_AUTHORITY_BINDINGS}
    spec = _load_json(workspace / SPEC_NAME, "release spec")
    for key, value in expected.items():
        if spec.get(key) != value:
            raise ReleaseFlowError(f"release spec {key} differs from release flow state")

    request = _load_json(workspace / REVIEW_REQUEST_NAME, "security review request")
    request_release = request.get("release")
    if not isinstance(request_release, dict):
        raise ReleaseFlowError("security review request has no release authority")
    for key, value in expected.items():
        if request_release.get(key) != value:
            raise ReleaseFlowError(
                f"security review request {key} differs from release flow state"
            )

    if state["phase"] in {"bundled", "publishing", "published", "deployed"}:
        release = _load_json(workspace / RELEASE_NAME, "release authority")
        for key, value in expected.items():
            if release.get(key) != value:
                raise ReleaseFlowError(
                    f"release authority {key} differs from release flow state"
                )
