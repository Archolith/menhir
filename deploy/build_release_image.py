#!/usr/bin/env python3
"""Build, verify, and optionally publish one release-labelled Menhir image."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Sequence

SHA256_RE = re.compile(r"[0-9a-f]{64}")
VERSION_RE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+-[0-9]+")
PUSH_DIGEST_RE = re.compile(r"digest:\s+(sha256:[0-9a-f]{64})")
LABELS = {
    "commit": "org.opencontainers.image.revision",
    "version": "org.opencontainers.image.version",
    "wheel_manifest_sha256": "org.archolith.menhir.wheel-manifest.sha256",
    "oauth_wheel_sha256": "org.archolith.oauth.wheel.sha256",
}


class BuildImageError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def oauth_wheel_sha256(wheelhouse: Path) -> str:
    manifest = wheelhouse / "SHA256SUMS"
    if not manifest.is_file() or manifest.is_symlink():
        raise BuildImageError(f"wheel manifest is missing or unsafe: {manifest}")
    matches: list[str] = []
    for number, raw in enumerate(manifest.read_text(encoding="ascii").splitlines(), 1):
        parts = raw.split(maxsplit=1)
        if len(parts) != 2 or SHA256_RE.fullmatch(parts[0]) is None:
            raise BuildImageError(f"malformed SHA256SUMS line {number}")
        relative = parts[1].lstrip("* ")
        if "/" in relative or "\\" in relative or relative in {"", ".", ".."}:
            raise BuildImageError(f"unsafe wheel path on SHA256SUMS line {number}")
        wheel = wheelhouse / relative
        if not wheel.is_file() or wheel.is_symlink():
            raise BuildImageError(f"manifest wheel is missing or unsafe: {relative}")
        actual = sha256_file(wheel)
        if actual != parts[0]:
            raise BuildImageError(f"wheel digest mismatch: {relative}")
        normalized = relative.lower().replace("-", "_")
        if normalized.startswith("archolith_oauth_") and normalized.endswith(".whl"):
            matches.append(actual)
    if len(matches) != 1:
        raise BuildImageError("wheelhouse must contain exactly one archolith_oauth wheel")
    return matches[0]


def git_commit(repo: Path) -> str:
    for args, label in [(["diff", "--quiet"], "tracked worktree"),
                        (["diff", "--cached", "--quiet"], "index")]:
        result = subprocess.run(["git", "-C", str(repo), *args], check=False)
        if result.returncode != 0:
            raise BuildImageError(f"Git {label} is not clean")
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    )
    commit = result.stdout.strip()
    if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise BuildImageError("Git HEAD is not a full commit digest")
    return commit


def build_command(*, repo: Path, dockerfile: Path, image_tag: str, python_base: str,
                  commit: str, version: str, wheel_manifest: str,
                  oauth_wheel: str) -> list[str]:
    return [
        "docker", "build", "--file", str(dockerfile),
        "--build-arg", f"PYTHON_BASE={python_base}",
        "--build-arg", f"RELEASE_COMMIT={commit}",
        "--build-arg", f"RELEASE_VERSION={version}",
        "--build-arg", f"WHEEL_MANIFEST_SHA256={wheel_manifest}",
        "--build-arg", f"OAUTH_WHEEL_SHA256={oauth_wheel}",
        "--tag", image_tag, str(repo),
    ]


def inspect_image(image_tag: str, expected: dict[str, str]) -> tuple[str, dict[str, str]]:
    result = subprocess.run(
        ["docker", "image", "inspect", image_tag],
        check=True, capture_output=True, text=True,
    )
    rows = json.loads(result.stdout)
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise BuildImageError("docker image inspect returned an unexpected shape")
    image_id = rows[0].get("Id")
    labels = (rows[0].get("Config") or {}).get("Labels") or {}
    if not isinstance(image_id, str) or not image_id.startswith("sha256:"):
        raise BuildImageError("built image ID is missing")
    actual = {name: labels.get(label) for name, label in LABELS.items()}
    if actual != expected:
        raise BuildImageError(f"built image labels do not match derived inputs: {actual!r}")
    return image_id, actual


def publish(image_tag: str) -> str:
    result = subprocess.run(
        ["docker", "push", image_tag], check=True, capture_output=True, text=True,
    )
    matches = PUSH_DIGEST_RE.findall(result.stdout + "\n" + result.stderr)
    if not matches:
        raise BuildImageError("docker push did not report a registry manifest digest")
    return matches[-1]


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def run(args: argparse.Namespace) -> dict[str, Any]:
    repo = args.repo.resolve()
    dockerfile = (repo / args.dockerfile).resolve()
    wheelhouse = (repo / args.wheelhouse).resolve()
    if VERSION_RE.fullmatch(args.version) is None:
        raise BuildImageError("version must match <major>.<minor>.<patch>-<sequence>")
    if "@" in args.image or args.image.endswith(":"):
        raise BuildImageError("image must be a repository name without a tag or digest")
    if re.search(r"@sha256:[0-9a-f]{64}$", args.python_base) is None:
        raise BuildImageError("python base must be digest-pinned")
    if not dockerfile.is_file() or not wheelhouse.is_dir():
        raise BuildImageError("Dockerfile or wheelhouse is missing")

    commit = git_commit(repo)
    wheel_manifest = sha256_file(wheelhouse / "SHA256SUMS")
    oauth_wheel = oauth_wheel_sha256(wheelhouse)
    image_tag = f"{args.image}:{args.version}"
    expected = {
        "commit": commit,
        "version": args.version,
        "wheel_manifest_sha256": wheel_manifest,
        "oauth_wheel_sha256": oauth_wheel,
    }
    command = build_command(
        repo=repo, dockerfile=dockerfile, image_tag=image_tag,
        python_base=args.python_base, commit=commit, version=args.version,
        wheel_manifest=wheel_manifest, oauth_wheel=oauth_wheel,
    )
    subprocess.run(command, check=True)
    image_id, labels = inspect_image(image_tag, expected)
    registry_digest = publish(image_tag) if args.push else None
    metadata = {
        "schema": 1,
        "image_tag": image_tag,
        "image_id": image_id,
        "registry_digest": registry_digest,
        "image_ref": f"{image_tag}@{registry_digest}" if registry_digest else None,
        "python_base": args.python_base,
        "labels": labels,
    }
    if args.output:
        atomic_json(args.output.resolve(), metadata)
    return metadata


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--version", required=True)
    value.add_argument("--image", required=True, help="repository name without tag")
    value.add_argument("--python-base", required=True)
    value.add_argument("--repo", type=Path, default=Path.cwd())
    value.add_argument("--dockerfile", type=Path, default=Path("deploy/Dockerfile"))
    value.add_argument("--wheelhouse", type=Path, default=Path("deploy/wheelhouse"))
    value.add_argument("--output", type=Path)
    value.add_argument("--push", action="store_true")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    try:
        metadata = run(parser().parse_args(argv))
    except (BuildImageError, OSError, subprocess.CalledProcessError, ValueError) as exc:
        print(f"release image build failed: {exc}")
        return 1
    print(json.dumps(metadata, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
