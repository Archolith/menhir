"""Release-note fragment snapshotting and binding validation."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .core import PUBLICATION_RECEIPT_NAME, SHA256_RE, ReleaseFlowError
from .fsio import _regular_file, _sha256


def _fragment_value(fragment: Any, name: str) -> Any:
    if isinstance(fragment, dict):
        return fragment.get(name)
    return getattr(fragment, name, None)


def _snapshot_fragments(fragments_dir: Path) -> list[dict[str, str]]:
    """Bind each prepared fragment to its directory entry and exact bytes."""
    bindings: list[dict[str, str]] = []
    try:
        entries = sorted(os.scandir(fragments_dir), key=lambda entry: entry.name)
    except OSError as exc:
        raise ReleaseFlowError(f"cannot read fragments directory: {fragments_dir}") from exc
    for entry in entries:
        if entry.is_symlink() or not entry.is_file(follow_symlinks=False) \
                or not entry.name.endswith(".json"):
            raise ReleaseFlowError(f"unsafe release-note fragment entry: {entry.name}")
        if entry.name == PUBLICATION_RECEIPT_NAME:
            raise ReleaseFlowError("release-note fragment uses the publication receipt name")
        path = _regular_file(Path(entry.path).resolve(), "release-note fragment")
        bindings.append({"name": entry.name, "sha256": _sha256(path)})
    return bindings


def _validate_fragment_bindings(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list) or not value:
        raise ReleaseFlowError("release flow has no prepared fragment bindings")
    bindings: list[dict[str, str]] = []
    names: set[str] = set()
    for binding in value:
        if not isinstance(binding, dict) or set(binding) != {"name", "sha256"}:
            raise ReleaseFlowError("release flow fragment binding is invalid")
        name = binding.get("name")
        digest = binding.get("sha256")
        if not isinstance(name, str) or Path(name).name != name \
                or not name.endswith(".json") or name == PUBLICATION_RECEIPT_NAME:
            raise ReleaseFlowError("release flow fragment name is unsafe")
        if name in names:
            raise ReleaseFlowError(f"duplicate release flow fragment binding: {name}")
        if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
            raise ReleaseFlowError(f"release flow fragment digest is invalid: {name}")
        names.add(name)
        bindings.append({"name": name, "sha256": digest})
    if bindings != sorted(bindings, key=lambda binding: binding["name"]):
        raise ReleaseFlowError("release flow fragment bindings are not sorted")
    return bindings
