"""Publication paths, receipts, and release-fragment archive verification."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .core import (
    FROZEN_ARTIFACT_KEYS,
    PUBLICATION_KIND,
    PUBLICATION_RECEIPT_NAME,
    SCHEMA,
    SHA256_RE,
    ReleaseFlowError,
)
from .fragments import _validate_fragment_bindings
from .fsio import _load_json, _regular_directory, _regular_file, _sha256


def _publication_paths(state: dict[str, Any]) -> tuple[Path, Path, Path, Path]:
    fragments_dir = Path(state["fragments_dir"])
    releases_dir = fragments_dir.parent / "releases"
    archive = releases_dir / state["release_id"]
    staging = releases_dir / (
        f".{state['release_id']}.{state['publication_nonce']}.publishing"
    )
    return fragments_dir, releases_dir, staging, archive


def _publication_receipt(state: dict[str, Any]) -> dict[str, Any]:
    artifacts: dict[str, str] = {}
    for key in FROZEN_ARTIFACT_KEYS:
        value = state.get(key)
        if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
            raise ReleaseFlowError(f"release is missing frozen artifact digest: {key}")
        artifacts[key] = value
    return {
        "schema": SCHEMA,
        "kind": PUBLICATION_KIND,
        "release_id": state["release_id"],
        "publication_nonce": state["publication_nonce"],
        "workspace": state["workspace"],
        "fragments_dir": state["fragments_dir"],
        "fragments": _validate_fragment_bindings(state["fragments"]),
        "artifacts": artifacts,
    }


def _verify_archive(
    archive: Path,
    state: dict[str, Any],
    *,
    receipt_sha256: str | None = None,
) -> str:
    _regular_directory(archive, "release fragment archive")
    bindings = _validate_fragment_bindings(state["fragments"])
    expected_names = {binding["name"] for binding in bindings}
    expected_names.add(PUBLICATION_RECEIPT_NAME)
    actual_names = {entry.name for entry in os.scandir(archive)}
    if actual_names != expected_names:
        raise ReleaseFlowError("release fragment archive contents are invalid")
    for binding in bindings:
        path = _regular_file(archive / binding["name"], "archived fragment")
        if path.parent != archive or _sha256(path) != binding["sha256"]:
            raise ReleaseFlowError(f"archived release-note fragment changed: {binding['name']}")
    receipt_path = _regular_file(
        archive / PUBLICATION_RECEIPT_NAME, "publication receipt"
    )
    if receipt_path.parent != archive:
        raise ReleaseFlowError("publication receipt escaped its release archive")
    if _load_json(receipt_path, "publication receipt") != _publication_receipt(state):
        raise ReleaseFlowError("publication receipt does not match the frozen release")
    actual_receipt_sha256 = _sha256(receipt_path)
    if receipt_sha256 is not None and actual_receipt_sha256 != receipt_sha256:
        raise ReleaseFlowError("publication receipt changed")
    return actual_receipt_sha256


def _verify_publication(state: dict[str, Any]) -> None:
    fragments_dir, releases_dir, staging, archive = _publication_paths(state)
    _regular_directory(releases_dir, "release archives directory")
    if staging.exists() or staging.is_symlink():
        raise ReleaseFlowError("published release still has an incomplete archive")
    for binding in _validate_fragment_bindings(state["fragments"]):
        source = fragments_dir / binding["name"]
        if source.exists() or source.is_symlink():
            raise ReleaseFlowError(
                f"published release-note fragment was replaced: {binding['name']}"
            )
    _verify_archive(
        archive,
        state,
        receipt_sha256=state["publication_receipt_sha256"],
    )
