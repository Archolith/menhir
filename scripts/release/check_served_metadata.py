"""Verify that a deployed Menhir serves the build this checkout/package expects.

Fetches ``<base-url>/readyz`` (public, unauthenticated) and compares ``build.version``,
``build.mcp_metadata_sha256`` and, when requested, ``build.commit`` with the locally
installed package.

Usage:
    python scripts/release/check_served_metadata.py https://menhir.example.com
    python scripts/release/check_served_metadata.py URL --expect-commit <sha> --expect-version 0.3.2

Exit codes: 0 all fields match, 1 mismatch, 2 fetch or parse error.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import urllib.error
import urllib.request
from typing import Any


def fetch_readyz(base_url: str, timeout: float) -> dict[str, Any]:
    """GET ``/readyz`` and return the parsed JSON body (503 bodies are still parsed)."""

    request = urllib.request.Request(
        base_url.rstrip("/") + "/readyz", headers={"Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read()
    body = json.loads(raw.decode("utf-8"))
    if not isinstance(body, dict):
        raise ValueError("readyz body is not a JSON object")
    return body


def _compare(name: str, expected: str, served: str) -> bool:
    ok = expected == served
    print(f"{name}: {'ok' if ok else 'MISMATCH'} expected={expected} served={served}")
    return ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("base_url")
    parser.add_argument("--expect-version")
    parser.add_argument("--expect-commit")
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args(argv)

    try:
        body = fetch_readyz(args.base_url, args.timeout)
        build = body["build"]
        if not isinstance(build, dict):
            raise ValueError("build block is not an object")
        served_version = str(build["version"])
        served_commit = str(build["commit"])
        served_digest = str(build["mcp_metadata_sha256"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"error: could not read build info from readyz ({type(exc).__name__})")
        return 2

    from menhir import __version__
    from menhir.mcp.served_metadata import served_mcp_metadata_digest

    expected_digest = asyncio.run(served_mcp_metadata_digest())
    results = [
        _compare("version", args.expect_version or __version__, served_version),
        _compare("mcp_metadata_sha256", expected_digest, served_digest),
    ]
    if args.expect_commit:
        results.append(_compare("commit", args.expect_commit, served_commit))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
