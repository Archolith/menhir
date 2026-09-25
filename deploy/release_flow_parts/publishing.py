"""The publication flow that archives frozen release metadata and notes."""

from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Any

from .core import LEGACY_STATE_KEYS, PUBLICATION_RECEIPT_NAME, ReleaseFlowError
from .fragments import _validate_fragment_bindings
from .fsio import (
    _atomic_json,
    _json_sha256,
    _regular_directory,
    _regular_file,
    _sha256,
    _workspace,
)
from .publication import _publication_paths, _publication_receipt, _verify_archive
from .verification import _load_state, _state_path, _verify_staged_files


def publish_flow(workspace: Path, confirmation: str) -> dict[str, Any]:
    """Publish frozen release metadata without deploying the install bundle."""
    workspace = _workspace(workspace)
    state = _load_state(workspace)
    if confirmation != state["release_id"]:
        raise ReleaseFlowError("publication confirmation must exactly match the release_id")
    if set(state) == LEGACY_STATE_KEYS:
        raise ReleaseFlowError(
            "legacy release flow can be inspected but has no prepared fragment bindings"
        )
    if state["phase"] == "published":
        _verify_staged_files(workspace, state)
        return state
    if state["phase"] not in {"bundled", "publishing"}:
        raise ReleaseFlowError("only a bundled release can be published")
    _verify_staged_files(workspace, state)

    bindings = _validate_fragment_bindings(state["fragments"])
    fragments_dir = Path(state["fragments_dir"])
    releases_dir = fragments_dir.parent / "releases"
    archive = releases_dir / state["release_id"]
    _regular_directory(fragments_dir, "prepared fragments directory")

    if state["phase"] == "bundled":
        for binding in bindings:
            source = _regular_file(
                fragments_dir / binding["name"], "prepared release-note fragment"
            )
            if _sha256(source) != binding["sha256"]:
                raise ReleaseFlowError(
                    f"prepared release-note fragment changed: {binding['name']}"
                )
        if releases_dir.exists() or releases_dir.is_symlink():
            _regular_directory(releases_dir, "release archives directory")
            if archive.exists() or archive.is_symlink():
                raise ReleaseFlowError(
                    "unexpected release archive exists before publication transaction"
                )
            prefix = f".{state['release_id']}."
            if any(
                entry.name.startswith(prefix) and entry.name.endswith(".publishing")
                for entry in os.scandir(releases_dir)
            ):
                raise ReleaseFlowError(
                    "unexpected incomplete archive exists before publication transaction"
                )
        state.update({
            "phase": "publishing",
            "publication_nonce": secrets.token_hex(16),
        })
        state["publication_receipt_sha256"] = _json_sha256(
            _publication_receipt(state)
        )
        _atomic_json(_state_path(workspace), state)

    fragments_dir, releases_dir, staging, archive = _publication_paths(state)
    if releases_dir.exists() or releases_dir.is_symlink():
        _regular_directory(releases_dir, "release archives directory")
    else:
        releases_dir.mkdir()
        _regular_directory(releases_dir, "release archives directory")

    if archive.exists() or archive.is_symlink():
        if staging.exists() or staging.is_symlink():
            raise ReleaseFlowError("both complete and incomplete release archives exist")
        for binding in bindings:
            source = fragments_dir / binding["name"]
            if source.exists() or source.is_symlink():
                raise ReleaseFlowError(
                    f"archived release-note fragment was replaced: {binding['name']}"
                )
        _verify_archive(
            archive,
            state,
            receipt_sha256=state["publication_receipt_sha256"],
        )
        state["phase"] = "published"
        _atomic_json(_state_path(workspace), state)
        return state

    if staging.exists() or staging.is_symlink():
        _regular_directory(staging, "incomplete release fragment archive")
    else:
        staging.mkdir()
    allowed_staging_names = {binding["name"] for binding in bindings}
    allowed_staging_names.add(PUBLICATION_RECEIPT_NAME)
    staging_names = {entry.name for entry in os.scandir(staging)}
    if not staging_names <= allowed_staging_names:
        raise ReleaseFlowError("incomplete release fragment archive has unknown entries")
    if PUBLICATION_RECEIPT_NAME in staging_names \
            and staging_names != allowed_staging_names:
        raise ReleaseFlowError("incomplete release fragment archive has a premature receipt")

    locations: list[tuple[dict[str, str], Path, Path]] = []
    for binding in bindings:
        source = fragments_dir / binding["name"]
        staged = staging / binding["name"]
        source_exists = source.exists() or source.is_symlink()
        staged_exists = staged.exists() or staged.is_symlink()
        if source_exists == staged_exists:
            qualifier = "both source and archive" if source_exists else "neither source nor archive"
            raise ReleaseFlowError(
                f"prepared fragment exists in {qualifier}: {binding['name']}"
            )
        current = source if source_exists else staged
        current = _regular_file(current, "prepared release-note fragment")
        if _sha256(current) != binding["sha256"]:
            raise ReleaseFlowError(
                f"prepared release-note fragment changed: {binding['name']}"
            )
        locations.append((binding, source, staged))

    if PUBLICATION_RECEIPT_NAME in staging_names:
        _verify_staged_files(workspace, state)
        _verify_archive(
            staging,
            state,
            receipt_sha256=state["publication_receipt_sha256"],
        )
    else:
        for binding, source, staged in locations:
            if staged.exists():
                continue
            os.replace(source, staged)
            try:
                if _sha256(_regular_file(staged, "archived release-note fragment")) \
                        != binding["sha256"]:
                    raise ReleaseFlowError(
                        f"prepared release-note fragment was replaced while publishing: "
                        f"{binding['name']}"
                    )
            except Exception:
                if not source.exists() and staged.exists():
                    os.replace(staged, source)
                raise

        _verify_staged_files(workspace, state)
        _atomic_json(staging / PUBLICATION_RECEIPT_NAME, _publication_receipt(state))
        _verify_archive(
            staging,
            state,
            receipt_sha256=state["publication_receipt_sha256"],
        )

    os.replace(staging, archive)
    _verify_archive(
        archive,
        state,
        receipt_sha256=state["publication_receipt_sha256"],
    )
    state["phase"] = "published"
    _atomic_json(_state_path(workspace), state)
    return state
