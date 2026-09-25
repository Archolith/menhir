"""Registry digest resolution for release candidate tags."""

from __future__ import annotations

import subprocess

from .base import PUSH_DIGEST_RE, REMOTE_DIGEST_RE, BuildImageError
from .evidenceio import _require_sha256


def _push_digest(image_tag: str) -> str:
    result = subprocess.run(
        ["docker", "push", image_tag], check=True, capture_output=True, text=True,
    )
    matches = PUSH_DIGEST_RE.findall(result.stdout + "\n" + result.stderr)
    if not matches:
        raise BuildImageError("docker push did not report a registry manifest digest")
    return matches[-1]


def remote_manifest_digest(image_tag: str) -> str | None:
    result = subprocess.run(
        ["docker", "buildx", "imagetools", "inspect", image_tag],
        check=False, capture_output=True, text=True,
    )
    if result.returncode != 0:
        message = (result.stdout + "\n" + result.stderr).lower()
        if any(marker in message for marker in ("manifest unknown", "no such manifest", "not found")):
            return None
        raise BuildImageError(f"could not inspect remote tag: {message.strip()}")
    matches = REMOTE_DIGEST_RE.findall(result.stdout)
    if len(matches) != 1:
        raise BuildImageError("remote manifest digest is missing or malformed")
    digest = matches[0]
    return _require_sha256(digest, "remote manifest digest", prefixed=True)
