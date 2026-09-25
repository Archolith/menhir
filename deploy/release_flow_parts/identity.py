"""Deterministic release-label derivation and verification."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .core import RELEASE_ID_PARTS_RE, VERSION_RE, ReleaseFlowError
from .fsio import _load_json, _regular_file


def next_release_id(prior_release: Path, version: str | None = None) -> dict[str, Any]:
    """Derive the next deterministic release label without reserving or mutating it."""
    prior_release = _regular_file(prior_release, "prior release authority")
    prior = _load_json(prior_release, "prior release authority")
    prior_id = prior.get("release_id")
    if not isinstance(prior_id, str):
        raise ReleaseFlowError("prior release identity is invalid")
    match = RELEASE_ID_PARTS_RE.fullmatch(prior_id)
    if match is None:
        raise ReleaseFlowError("prior release identity is invalid")
    target_version = version or match.group("version")
    if VERSION_RE.fullmatch(target_version) is None:
        raise ReleaseFlowError("release version must match <major>.<minor>.<patch>")
    prior_version = tuple(int(part) for part in match.group("version").split("."))
    requested_version = tuple(int(part) for part in target_version.split("."))
    if requested_version < prior_version:
        raise ReleaseFlowError("release version cannot move backwards")
    sequence = int(match.group("sequence")) + 1 \
        if target_version == match.group("version") else 1
    return {
        "schema": 1,
        "kind": "menhir-next-release-id",
        "prior_release_id": prior_id,
        "version": target_version,
        "release_id": f"menhir-prod-{target_version}-{sequence}",
    }


def _verify_next_release_id(spec: dict[str, Any]) -> None:
    release_id = spec.get("release_id")
    if not isinstance(release_id, str):
        raise ReleaseFlowError("release spec identity is invalid")
    match = RELEASE_ID_PARTS_RE.fullmatch(release_id)
    if match is None:
        raise ReleaseFlowError("release spec identity is invalid")
    if spec.get("initial_release") is True:
        if int(match.group("sequence")) != 1:
            raise ReleaseFlowError("initial release sequence must be 1")
        return
    prior_path = spec.get("prior_release")
    if not isinstance(prior_path, str):
        raise ReleaseFlowError("non-initial release has no prior release authority")
    expected = next_release_id(Path(prior_path), match.group("version"))["release_id"]
    if release_id != expected:
        raise ReleaseFlowError(
            f"release ID must be the generated next label: {expected}"
        )
