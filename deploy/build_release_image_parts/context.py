"""Git-derived build inputs, the verified wheelhouse, and image inspection."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tarfile
import tempfile
from contextlib import contextmanager
from pathlib import Path

from .base import DIGEST_RE, LABELS, SHA256_RE, BuildImageError, sha256_file


def oauth_wheel_sha256(wheelhouse: Path) -> str:
    manifest = wheelhouse / "SHA256SUMS"
    if not manifest.is_file() or manifest.is_symlink():
        raise BuildImageError(f"wheel manifest is missing or unsafe: {manifest}")
    matches: list[str] = []
    manifested: set[str] = set()
    for number, raw in enumerate(manifest.read_text(encoding="ascii").splitlines(), 1):
        parts = raw.split(maxsplit=1)
        if len(parts) != 2 or SHA256_RE.fullmatch(parts[0]) is None:
            raise BuildImageError(f"malformed SHA256SUMS line {number}")
        relative = parts[1].lstrip("* ")
        if "/" in relative or "\\" in relative or relative in {"", ".", ".."}:
            raise BuildImageError(f"unsafe wheel path on SHA256SUMS line {number}")
        if relative in manifested:
            raise BuildImageError(f"duplicate wheel manifest entry: {relative}")
        manifested.add(relative)
        wheel = wheelhouse / relative
        if not wheel.is_file() or wheel.is_symlink():
            raise BuildImageError(f"manifest wheel is missing or unsafe: {relative}")
        actual = sha256_file(wheel)
        if actual != parts[0]:
            raise BuildImageError(f"wheel digest mismatch: {relative}")
        normalized = relative.lower().replace("-", "_")
        if normalized.startswith("archolith_oauth_") and normalized.endswith(".whl"):
            matches.append(actual)
    actual = set()
    for entry in wheelhouse.iterdir():
        if entry.name == "SHA256SUMS":
            continue
        if entry.is_symlink() or not entry.is_file() or entry.suffix != ".whl":
            raise BuildImageError(f"unexpected or unsafe wheelhouse entry: {entry.name}")
        actual.add(entry.name)
    if actual != manifested:
        raise BuildImageError(
            "wheel manifest closure mismatch: "
            f"missing={sorted(manifested - actual)}, extra={sorted(actual - manifested)}"
        )
    if len(matches) != 1:
        raise BuildImageError("wheelhouse must contain exactly one archolith_oauth wheel")
    return matches[0]


def git_source_date_epoch(repo: Path, commit: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), "show", "-s", "--format=%ct", commit],
        check=True, capture_output=True, text=True,
    )
    value = result.stdout.strip()
    if re.fullmatch(r"[1-9][0-9]{8,}", value) is None:
        raise BuildImageError("Git commit timestamp is malformed")
    return value


def _repository_relative(repo: Path, path: Path, label: str) -> Path:
    try:
        relative = path.resolve(strict=True).relative_to(repo.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise BuildImageError(f"{label} must be inside the repository") from exc
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise BuildImageError(f"{label} is not a canonical repository path")
    return relative


@contextmanager
def committed_build_context(
    repo: Path, commit: str, dockerfile: Path, wheelhouse: Path,
):
    """Yield a Docker context made from the commit plus the verified wheelhouse."""
    dockerfile_relative = _repository_relative(repo, dockerfile, "Dockerfile")
    wheelhouse_relative = _repository_relative(repo, wheelhouse, "wheelhouse")
    oauth_wheel_sha256(wheelhouse)
    with tempfile.TemporaryDirectory(prefix="menhir-release-context-") as temporary:
        root = Path(temporary)
        archive_path = root / "source.tar"
        context = root / "context"
        context.mkdir()
        subprocess.run(
            ["git", "-C", str(repo), "archive", "--format=tar", "--output",
             str(archive_path), commit],
            check=True,
        )
        with tarfile.open(archive_path, mode="r:") as archive:
            archive.extractall(context, filter="data")
        staged_wheelhouse = context / wheelhouse_relative
        if staged_wheelhouse.exists() or staged_wheelhouse.is_symlink():
            raise BuildImageError("committed source unexpectedly contains deploy/wheelhouse")
        staged_wheelhouse.mkdir(parents=True)
        for entry in wheelhouse.iterdir():
            shutil.copyfile(entry, staged_wheelhouse / entry.name, follow_symlinks=False)
        oauth_wheel_sha256(staged_wheelhouse)
        staged_dockerfile = context / dockerfile_relative
        if not staged_dockerfile.is_file() or staged_dockerfile.is_symlink():
            raise BuildImageError("committed Dockerfile is missing or unsafe")
        yield context, staged_dockerfile


def build_command(*, repo: Path, dockerfile: Path, image_tag: str, python_base: str,
                  commit: str, version: str, wheel_manifest: str,
                  oauth_wheel: str, source_date_epoch: str) -> list[str]:
    return [
        "docker", "build", "--file", str(dockerfile),
        "--network", "none", "--pull=false", "--no-cache",
        "--platform", "linux/amd64", "--provenance=false", "--sbom=false",
        "--build-arg", f"PYTHON_BASE={python_base}",
        "--build-arg", f"SOURCE_DATE_EPOCH={source_date_epoch}",
        "--build-arg", f"RELEASE_COMMIT={commit}",
        "--build-arg", f"RELEASE_VERSION={version}",
        "--build-arg", f"WHEEL_MANIFEST_SHA256={wheel_manifest}",
        "--build-arg", f"OAUTH_WHEEL_SHA256={oauth_wheel}",
        "--tag", image_tag, str(repo),
    ]


def inspect_image(image_tag: str, expected: dict[str, str]) -> tuple[str, dict[str, str], str]:
    result = subprocess.run(
        ["docker", "image", "inspect", image_tag],
        check=True, capture_output=True, text=True,
    )
    rows = json.loads(result.stdout)
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise BuildImageError("docker image inspect returned an unexpected shape")
    image_id = rows[0].get("Id")
    config = rows[0].get("Config")
    if not isinstance(image_id, str) or DIGEST_RE.fullmatch(image_id) is None:
        raise BuildImageError("built image ID is missing or malformed")
    if not isinstance(config, dict):
        raise BuildImageError("built image config is missing")
    labels = config.get("Labels") or {}
    if not isinstance(labels, dict):
        raise BuildImageError("built image labels are malformed")
    actual = {name: labels.get(label) for name, label in LABELS.items()}
    if actual != expected:
        raise BuildImageError(f"built image labels do not match derived inputs: {actual!r}")
    encoded = json.dumps(
        config, ensure_ascii=True, separators=(",", ":"), sort_keys=True,
    ).encode("ascii")
    return image_id, actual, hashlib.sha256(encoded).hexdigest()
