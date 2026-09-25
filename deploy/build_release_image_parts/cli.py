"""Build, seal, verify, and publish one release-labelled Menhir image."""

# The module docstring above is deliberately byte-identical to
# deploy/build_release_image.py's docstring: parser() feeds it to argparse as
# the --help description, so it must not diverge from the facade module's.
from __future__ import annotations

import argparse
import re
from pathlib import Path

from .base import CANONICAL_SOURCE_REPOSITORY, VERSION_RE, BuildImageError


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--mode", choices=("build", "verify", "publish"), default="build")
    value.add_argument("--version", required=True)
    value.add_argument("--image", required=True, help="repository name without tag")
    value.add_argument("--python-base", required=True)
    value.add_argument("--repo", type=Path, default=Path.cwd())
    value.add_argument("--dockerfile", type=Path, default=Path("deploy/Dockerfile"))
    value.add_argument("--wheelhouse", type=Path, default=Path("deploy/wheelhouse"))
    value.add_argument("--expected-commit")
    value.add_argument("--source-repository", required=True)
    value.add_argument("--output", type=Path)
    value.add_argument("--metadata", type=Path)
    value.add_argument("--identity", type=Path)
    value.add_argument("--image-archive", type=Path)
    value.add_argument("--expected-identity-sha256")
    value.add_argument("--syft-image")
    value.add_argument("--grype-image")
    return value


def _validate_common_args(args: argparse.Namespace) -> None:
    if VERSION_RE.fullmatch(args.version) is None:
        raise BuildImageError("version must match <major>.<minor>.<patch>-<sequence>")
    remainder = args.image.removeprefix("ghcr.io/")
    if (remainder == args.image or "/" not in remainder or "@" in args.image
            or ":" in remainder):
        raise BuildImageError("image must be an untagged ghcr.io repository")
    if re.search(r"@sha256:[0-9a-f]{64}$", args.python_base) is None:
        raise BuildImageError("python base must be digest-pinned")
    if args.expected_commit and re.fullmatch(r"[0-9a-f]{40}", args.expected_commit) is None:
        raise BuildImageError("expected commit must be a full commit digest")
    if args.source_repository != CANONICAL_SOURCE_REPOSITORY:
        raise BuildImageError("source repository must be the canonical Archolith/menhir repository")
