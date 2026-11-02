#!/usr/bin/env python3
"""Decisions for the tag-triggered release in .github/workflows/publish-pypi.yml.

Standard library only: this runs on a bare runner before anything is installed. Every
unexpected answer from PyPI or the registry stops the release.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path

PYPI_PROJECT = "archolith-menhir"
PYPI_FILE_HOST = "files.pythonhosted.org"
REGISTRY = "ghcr.io"

TAG_RE = re.compile(r"v([0-9]+\.[0-9]+\.[0-9]+)")
DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
PYTHON_BASE_RE = re.compile(r"[a-z0-9][a-z0-9./_:-]*@sha256:[0-9a-f]{64}")
IMAGE_RE = re.compile(r"ghcr\.io/([a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._-]*)")
IMAGE_TAG_RE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
MANIFEST_ACCEPT = (
    "application/vnd.oci.image.index.v1+json, "
    "application/vnd.oci.image.manifest.v1+json, "
    "application/vnd.docker.distribution.manifest.v2+json, "
    "application/vnd.docker.distribution.manifest.list.v2+json"
)


class ReleasePipelineError(RuntimeError):
    pass


# (method, url, headers, body) -> (status, lower-cased headers, body)
Http = Callable[[str, str, dict[str, str], bytes | None], tuple[int, dict[str, str], bytes]]


def urllib_http(
    method: str, url: str, headers: dict[str, str], body: bytes | None,
) -> tuple[int, dict[str, str], bytes]:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return (
                response.status,
                {key.lower(): value for key, value in response.headers.items()},
                response.read(),
            )
    except urllib.error.HTTPError as error:
        return (
            error.code,
            {key.lower(): value for key, value in error.headers.items()},
            error.read(),
        )


def parse_release_tag(tag: str) -> str:
    match = TAG_RE.fullmatch(tag)
    if match is None:
        raise ReleasePipelineError(f"release tag must be vX.Y.Z, got {tag!r}")
    return match.group(1)


def require_run_on_tag(github_ref: str, tag: str) -> None:
    # The attestation records the commit the run started from, so a retry dispatched from a
    # branch would sign for the wrong commit and fail verification after publishing.
    if github_ref != f"refs/tags/{tag}":
        raise ReleasePipelineError(
            f"this run is on {github_ref}, not {tag}; retry with "
            f"`gh workflow run publish-pypi.yml --ref {tag} -f tag={tag}`"
        )


def read_python_base(path: Path) -> str:
    lines = [
        line.strip() for line in path.read_text(encoding="ascii").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if len(lines) != 1 or PYTHON_BASE_RE.fullmatch(lines[0]) is None:
        raise ReleasePipelineError(f"{path} must hold one digest-pinned image reference")
    return lines[0]


def _repository(image: str) -> str:
    match = IMAGE_RE.fullmatch(image)
    if match is None:
        raise ReleasePipelineError(f"image must be an untagged ghcr.io repository: {image!r}")
    return match.group(1)


def _registry_token(
    http: Http, repository: str, *, actions: str, credential: str | None = None,
) -> str:
    query = urllib.parse.urlencode({
        "service": REGISTRY, "scope": f"repository:{repository}:{actions}",
    })
    headers: dict[str, str] = {}
    if credential is not None:
        headers["Authorization"] = "Basic " + base64.b64encode(credential.encode()).decode()
    status, _, body = http("GET", f"https://{REGISTRY}/token?{query}", headers, None)
    if status != 200:
        raise ReleasePipelineError(f"registry token request returned HTTP {status}")
    token = json.loads(body).get("token")
    if not isinstance(token, str) or not token:
        raise ReleasePipelineError("registry returned no token")
    return token


def _tag_digest(http: Http, repository: str, tag: str, token: str) -> str | None:
    status, headers, _ = http(
        "HEAD",
        f"https://{REGISTRY}/v2/{repository}/manifests/{tag}",
        {"Authorization": f"Bearer {token}", "Accept": MANIFEST_ACCEPT},
        None,
    )
    if status == 404:
        return None
    if status != 200:
        raise ReleasePipelineError(f"registry lookup of tag {tag} returned HTTP {status}")
    digest = headers.get("docker-content-digest", "")
    if DIGEST_RE.fullmatch(digest) is None:
        raise ReleasePipelineError(f"registry returned a malformed digest for tag {tag}")
    return digest


def existing_image_digest(http: Http, image: str, tag: str) -> str | None:
    repository = _repository(image)
    return _tag_digest(http, repository, tag, _registry_token(http, repository, actions="pull"))


def tag_image(http: Http, image: str, tag: str, digest: str, credential: str) -> str:
    """Point ``tag`` at ``digest`` by storing the same manifest bytes, so the digest and its
    attestations stay valid. Never moves a tag that already names another digest."""
    repository = _repository(image)
    if IMAGE_TAG_RE.fullmatch(tag) is None:
        raise ReleasePipelineError(f"image tag must be X.Y.Z, got {tag!r}")
    if DIGEST_RE.fullmatch(digest) is None:
        raise ReleasePipelineError(f"image digest is malformed: {digest!r}")
    token = _registry_token(http, repository, actions="pull,push", credential=credential)
    current = _tag_digest(http, repository, tag, token)
    if current == digest:
        return "already tagged"
    if current is not None:
        raise ReleasePipelineError(
            f"{image}:{tag} already names {current}; refusing to move it to {digest}"
        )
    status, headers, manifest = http(
        "GET",
        f"https://{REGISTRY}/v2/{repository}/manifests/{digest}",
        {"Authorization": f"Bearer {token}", "Accept": MANIFEST_ACCEPT},
        None,
    )
    if status != 200:
        raise ReleasePipelineError(f"reading manifest {digest} returned HTTP {status}")
    if "sha256:" + hashlib.sha256(manifest).hexdigest() != digest:
        raise ReleasePipelineError(f"manifest bytes do not hash to {digest}")
    content_type = headers.get("content-type", "")
    if not content_type:
        raise ReleasePipelineError("registry returned a manifest without a media type")
    status, _, _ = http(
        "PUT",
        f"https://{REGISTRY}/v2/{repository}/manifests/{tag}",
        {"Authorization": f"Bearer {token}", "Content-Type": content_type},
        manifest,
    )
    if status not in {200, 201}:
        raise ReleasePipelineError(f"writing tag {tag} returned HTTP {status}")
    written = _tag_digest(http, repository, tag, token)
    if written != digest:
        raise ReleasePipelineError(f"{image}:{tag} names {written} after writing {digest}")
    return "tagged"


def adopt_published_distributions(http: Http, version: str, dist_dir: Path) -> bool:
    """Return whether PyPI already has ``version``. If it does, replace ``dist_dir`` with the
    published files, so the GitHub release carries what PyPI serves and not a rebuild."""
    status, _, body = http("GET", f"https://pypi.org/pypi/{PYPI_PROJECT}/{version}/json", {}, None)
    if status == 404:
        return False
    if status != 200:
        raise ReleasePipelineError(f"PyPI lookup of {version} returned HTTP {status}")
    published = json.loads(body).get("urls") or []
    local = sorted(path.name for path in dist_dir.iterdir() if path.is_file())
    if sorted(entry.get("filename", "") for entry in published) != local:
        raise ReleasePipelineError(
            f"PyPI has a different file set for {version} than this build ({local}); "
            "inspect the release by hand"
        )
    with tempfile.TemporaryDirectory() as staging:
        downloaded: list[tuple[Path, str]] = []
        for entry in published:
            name, url = entry["filename"], entry["url"]
            parsed = urllib.parse.urlparse(url)
            if parsed.scheme != "https" or parsed.hostname != PYPI_FILE_HOST:
                raise ReleasePipelineError(f"unexpected download location for {name}: {url}")
            status, _, content = http("GET", url, {}, None)
            if status != 200:
                raise ReleasePipelineError(f"downloading {name} returned HTTP {status}")
            if hashlib.sha256(content).hexdigest() != entry["digests"]["sha256"]:
                raise ReleasePipelineError(f"{name} does not match the digest PyPI reports")
            path = Path(staging) / name
            path.write_bytes(content)
            downloaded.append((path, name))
        for path, name in downloaded:
            (dist_dir / name).write_bytes(path.read_bytes())
    return True


def _write_outputs(values: dict[str, str]) -> None:
    lines = [f"{key}={value}\n" for key, value in values.items()]
    target = os.environ.get("GITHUB_OUTPUT")
    if target:
        with Path(target).open("a", encoding="ascii") as output:
            output.writelines(lines)
    else:
        sys.stdout.writelines(lines)


def _cmd_plan(args: argparse.Namespace, http: Http) -> None:
    require_run_on_tag(args.github_ref, args.tag)
    version = parse_release_tag(args.tag)
    _write_outputs({
        "tag": args.tag,
        "version": version,
        "image_version": f"{version}-1",
        "python_base": read_python_base(Path(args.python_base_file)),
        "existing_image_digest": existing_image_digest(http, args.image, version) or "",
    })


def _cmd_pypi_state(args: argparse.Namespace, http: Http) -> None:
    already = adopt_published_distributions(http, args.version, Path(args.dist_dir))
    _write_outputs({"already_published": "true" if already else "false"})


def _cmd_tag_image(args: argparse.Namespace, http: Http) -> None:
    credential = f"{os.environ['GITHUB_ACTOR']}:{os.environ['GH_TOKEN']}"
    result = tag_image(http, args.image, args.tag, args.digest, credential)
    print(f"{args.image}:{args.tag} -> {args.digest}: {result}")


def main(argv: list[str] | None = None, http: Http = urllib_http) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    plan = commands.add_parser("plan")
    plan.add_argument("--tag", required=True)
    plan.add_argument("--github-ref", required=True)
    plan.add_argument("--image", required=True)
    plan.add_argument("--python-base-file", required=True)
    plan.set_defaults(handler=_cmd_plan)

    pypi_state = commands.add_parser("pypi-state")
    pypi_state.add_argument("--version", required=True)
    pypi_state.add_argument("--dist-dir", required=True)
    pypi_state.set_defaults(handler=_cmd_pypi_state)

    image_tag = commands.add_parser("tag-image")
    image_tag.add_argument("--image", required=True)
    image_tag.add_argument("--tag", required=True)
    image_tag.add_argument("--digest", required=True)
    image_tag.set_defaults(handler=_cmd_tag_image)

    args = parser.parse_args(argv)
    try:
        args.handler(args, http)
    except ReleasePipelineError as error:
        print(f"::error title=Release pipeline::{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
