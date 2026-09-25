"""Client policy, production environment, and provenance evidence validation."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from menhir.access_contract import validate_access_contract

from release_author_constants import SHA256_RE
from release_author_io import _exact, _load_json


def _validate_policy_env_binding(policy_path: Path, env_path: Path) -> None:
    policy = _load_json(policy_path, "client policy")
    if policy.get("version") != 2:
        raise ValueError("production client policy must use version 2 access contract")
    validate_access_contract(policy.get("access_contract"), policy.get("clients"))
    declared = policy.get("canonical_digest")
    if not isinstance(declared, str) or not SHA256_RE.fullmatch(declared):
        raise ValueError("client policy canonical_digest is invalid")
    canonical = dict(policy)
    canonical.pop("canonical_digest", None)
    actual = hashlib.sha256(json.dumps(
        canonical,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")).hexdigest()
    if declared != actual:
        raise ValueError("client policy canonical_digest does not match its payload")

    try:
        lines = env_path.read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ValueError("production environment must be ASCII") from exc
    prefix = "MENHIR_CLIENT_POLICY_DIGEST="
    matches = [line.removeprefix(prefix) for line in lines if line.startswith(prefix)]
    if matches != [declared]:
        raise ValueError(
            "production environment must bind MENHIR_CLIENT_POLICY_DIGEST "
            "to the client policy canonical_digest"
        )
    mode_prefix = "MENHIR_CANONICAL_SELF_BINDING_MODE="
    modes = [
        line.removeprefix(mode_prefix)
        for line in lines
        if line.startswith(mode_prefix)
    ]
    if len(modes) != 1 or modes[0] not in {"off", "observe", "enforce"}:
        raise ValueError(
            "production environment must bind exactly one "
            "MENHIR_CANONICAL_SELF_BINDING_MODE=off|observe|enforce"
        )


def _validate_provenance(
    path: Path,
    repos: dict[str, str],
    repo_remotes: dict[str, str],
    images: dict[str, str],
    oauth_sha: str,
    wheel_manifest_sha: str,
    docker_manifest_sha: str,
) -> str:
    raw = path.read_bytes()
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid provenance JSON") from exc
    expected_keys = frozenset({
        "schema", "repos", "repo_remotes", "images", "oauth_wheel_sha256",
        "wheel_manifest_sha256", "dockerfile_wheel_manifest_sha256",
    })
    _exact(value, expected_keys, "provenance")
    expected = {
        "schema": 1,
        "repos": repos,
        "repo_remotes": repo_remotes,
        "images": images,
        "oauth_wheel_sha256": oauth_sha,
        "wheel_manifest_sha256": wheel_manifest_sha,
        "dockerfile_wheel_manifest_sha256": docker_manifest_sha,
    }
    if value != expected:
        raise ValueError("provenance does not bind the exact release inputs")
    return hashlib.sha256(raw).hexdigest()

