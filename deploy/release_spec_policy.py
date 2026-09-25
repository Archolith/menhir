"""Client policy, production.env rendering, wheelhouse and output validation."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import verify_wheelhouse

from release_spec_constants import (
    ENV_KEY_RE,
    PLACEHOLDER_RE,
    SECRET_KEY_RE,
    SECRET_VALUE_RE,
    SHA256_RE,
)
from release_spec_errors import ReleaseSpecError
from release_spec_io import _directory, _regular, _sha256, _unique_pairs


def _canonical_json_digest(value: dict[str, Any]) -> str:
    canonical = dict(value)
    canonical.pop("canonical_digest", None)
    return hashlib.sha256(json.dumps(
        canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")).hexdigest()


def _validate_policy(data: bytes) -> str:
    try:
        value = json.loads(
            data.decode("utf-8"), object_pairs_hook=_unique_pairs
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseSpecError("client policy is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict) or value.get("version") != 2:
        raise ReleaseSpecError("client policy must be a version 2 object")
    declared = value.get("canonical_digest")
    if not isinstance(declared, str) or not SHA256_RE.fullmatch(declared):
        raise ReleaseSpecError("client policy canonical_digest is invalid")
    if declared != _canonical_json_digest(value):
        raise ReleaseSpecError("client policy canonical digest mismatch")
    return declared


def _reject_secret_material(value: Any, label: str) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            identifier = key.endswith("_secret_version")
            if SECRET_KEY_RE.search(key) and not identifier and (
                item not in (None, "", False)
            ):
                raise ReleaseSpecError(
                    f"secret-looking config key in {label}: {key}"
                )
            _reject_secret_material(item, f"{label}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_secret_material(item, f"{label}[{index}]")
    elif isinstance(value, str) and SECRET_VALUE_RE.search(value):
        raise ReleaseSpecError(f"secret-looking value in {label}")


def _render_env(path: Path, replacements: dict[str, str]) -> str:
    try:
        lines = path.read_text(encoding="ascii").splitlines()
    except UnicodeError as exc:
        raise ReleaseSpecError("baseline production.env must be ASCII") from exc
    seen: set[str] = set()
    rendered: list[str] = []
    for number, line in enumerate(lines, start=1):
        if not line or line.startswith("#"):
            rendered.append(line)
            continue
        if "=" not in line:
            raise ReleaseSpecError(f"invalid production.env line {number}")
        key, value = line.split("=", 1)
        if not ENV_KEY_RE.fullmatch(key):
            raise ReleaseSpecError(
                f"invalid production.env key on line {number}"
            )
        if key in seen:
            raise ReleaseSpecError(f"duplicate production.env key: {key}")
        seen.add(key)
        if SECRET_KEY_RE.search(key) and value:
            raise ReleaseSpecError(f"secret-looking production.env key: {key}")
        rendered.append(f"{key}={replacements.get(key, value)}")
    missing = sorted(set(replacements) - seen)
    if missing:
        raise ReleaseSpecError(
            f"production.env missing replacement keys: {missing}"
        )
    text = "\n".join(rendered) + "\n"
    if PLACEHOLDER_RE.search(text):
        raise ReleaseSpecError("production.env contains a placeholder")
    if SECRET_VALUE_RE.search(text):
        raise ReleaseSpecError("production.env contains secret-looking material")
    return text


def _wheelhouse(path_value: Any) -> tuple[Path, Path, Path, list[dict[str, Any]]]:
    path = _directory(path_value, "evidence.wheelhouse")
    entries = list(path.iterdir())
    if any(item.is_symlink() for item in entries):
        raise ReleaseSpecError("wheelhouse must not contain symlinks")
    wheels = sorted(
        item for item in entries if item.is_file() and item.suffix == ".whl"
    )
    oauth = [item for item in wheels if item.name.startswith("archolith_oauth-")]
    if len(oauth) != 1:
        raise ReleaseSpecError(
            "wheelhouse must contain exactly one archolith_oauth wheel"
        )
    manifest = _regular(path / "SHA256SUMS", "wheelhouse SHA256SUMS")
    try:
        verify_wheelhouse.verify(
            path, manifest, _sha256(manifest), _sha256(oauth[0])
        )
    except ValueError as exc:
        raise ReleaseSpecError(f"wheelhouse validation failed: {exc}") from exc
    records = [
        {
            "filename": item.name,
            "sha256": _sha256(item),
            "size": item.stat().st_size,
        }
        for item in wheels
    ]
    return path, oauth[0], manifest, records


def _validate_output(
    inputs: dict[str, Any], output_path: Path
) -> tuple[Path, Path]:
    workspace = _directory(
        inputs["release_workspace_root"], "release_workspace_root"
    )
    if not output_path.is_absolute():
        raise ReleaseSpecError("output_path must be absolute")
    if output_path.is_symlink():
        raise ReleaseSpecError("output_path must not be a symlink")
    if output_path.name != "release-spec.json" or output_path.parent != workspace:
        raise ReleaseSpecError(
            "output_path must be release_workspace_root/release-spec.json"
        )
    return workspace, workspace / "release-spec-inputs"
