"""Verify that a deployed Menhir serves the build this checkout/package expects.

Fetches ``<base-url>/readyz`` (served by the production surface only) and compares
``build.version``, ``build.mcp_metadata_sha256`` and, when requested, ``build.commit`` with the
locally installed package. Run it from a checkout with menhir installed at the version the server
should run: the expected digest is computed locally.

The server returns the build block only to a caller with a valid access token, read here from
``MENHIR_READYZ_TOKEN``. It is sent only over HTTPS or to loopback, and redirects are not followed.

Usage:
    python scripts/release/check_served_metadata.py https://menhir.example.com
    python scripts/release/check_served_metadata.py URL --expect-commit <sha> --expect-version <version>
    python scripts/release/check_served_metadata.py URL --snapshot-mode receive

Exit codes: 0 every compared field matches, 1 a compared field differs, 2 nothing differed but
a check could not be made (fetch, parse, argument, or local package error, or a server digest
that is unavailable).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

DESCRIPTION = "Check a deployed Menhir's /readyz build block against the local package."
EXIT_MATCH, EXIT_MISMATCH, EXIT_ERROR = 0, 1, 2
TOKEN_ENV = "MENHIR_READYZ_TOKEN"
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_COMMIT_RE = re.compile(r"[0-9a-fA-F]{7,40}")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_BUILD_FIELDS = ("version", "commit", "mcp_metadata_sha256")


class CheckError(Exception):
    """A check could not be made; reported with exit code 2."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """urllib re-sends Authorization on a redirect, even to another host; never follow one."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def _read_token() -> str | None:
    return os.environ.get(TOKEN_ENV, "").strip() or None


def _require_safe_token_url(base_url: str) -> None:
    parts = urllib.parse.urlsplit(base_url)
    if parts.scheme != "https" and parts.hostname not in _LOOPBACK_HOSTS:
        raise CheckError(f"{TOKEN_ENV} is sent only over HTTPS or to loopback")


def fetch_readyz(base_url: str, timeout: float, token: str | None = None) -> dict[str, Any]:
    """GET ``/readyz`` and return the parsed JSON body (503 bodies are still parsed)."""

    headers = {"Accept": "application/json"}
    if token:
        _require_safe_token_url(base_url)
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(base_url.rstrip("/") + "/readyz", headers=headers)
    opener = urllib.request.build_opener(_NoRedirect) if token else urllib.request.build_opener()
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read()
    body = json.loads(raw.decode("utf-8"))
    if not isinstance(body, dict):
        raise ValueError("readyz body is not a JSON object")
    return body


def read_build(body: dict[str, Any], *, token_sent: bool = False) -> dict[str, str]:
    """Return the served build fields, refusing a missing block or a non-string field."""

    build = body.get("build")
    if not isinstance(build, dict):
        if token_sent:
            raise CheckError(
                "readyz returned no build block: the server did not accept the token, or it is "
                "an older server, or not the production surface"
            )
        raise CheckError(f"readyz returned no build block: set {TOKEN_ENV} to a valid access token")
    fields: dict[str, str] = {}
    for name in _BUILD_FIELDS:
        value = build.get(name)
        if not isinstance(value, str) or not value:
            raise CheckError(f"readyz build.{name} is missing or not a string")
        fields[name] = value
    return fields


def _compare(name: str, expected: str, served: str, *, ok: bool | None = None) -> bool:
    ok = expected == served if ok is None else ok
    print(f"{name}: {'ok' if ok else 'MISMATCH'} expected={expected} served={served}")
    return ok


def _local_metadata(snapshot_mode: str | None) -> tuple[str, Any]:
    """Compute the local digest; *snapshot_mode* applies only while the catalog is built."""

    from menhir.config.snapshot_mode import (
        SNAPSHOT_RECEIVE_MODE_ENV,
        SnapshotReceiveMode,
        snapshot_receive_mode,
    )
    from menhir.mcp import served_metadata

    previous = os.environ.get(SNAPSHOT_RECEIVE_MODE_ENV)
    if snapshot_mode is not None:
        choices = [mode.value for mode in SnapshotReceiveMode]
        if snapshot_mode.strip().lower() not in choices:
            raise CheckError(f"--snapshot-mode must be one of: {', '.join(choices)}")
        os.environ[SNAPSHOT_RECEIVE_MODE_ENV] = snapshot_mode.strip().lower()
    try:
        # registered_tools() reads the mode when the catalog is built, not at import.
        mode = snapshot_receive_mode().value
        metadata = asyncio.run(served_metadata.compute_metadata())
    finally:
        if previous is None:
            os.environ.pop(SNAPSHOT_RECEIVE_MODE_ENV, None)
        else:
            os.environ[SNAPSHOT_RECEIVE_MODE_ENV] = previous
    return mode, metadata


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument("base_url")
    parser.add_argument("--expect-version")
    parser.add_argument("--expect-commit", help="7-40 hex characters; matched as a prefix")
    parser.add_argument(
        "--snapshot-mode",
        help="snapshot receive mode the server runs (off, receive, shadow, write); "
        "default: this process's MENHIR_SNAPSHOT_RECEIVE_MODE",
    )
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args(argv)
    try:
        return _check(args)
    except CheckError as exc:
        print(f"error: {exc}")
        return EXIT_ERROR


def _check(args: argparse.Namespace) -> int:
    if args.expect_commit is not None and not _COMMIT_RE.fullmatch(args.expect_commit):
        raise CheckError("--expect-commit must be 7-40 hex characters")
    if args.expect_version is not None and not args.expect_version.strip():
        raise CheckError("--expect-version must not be empty")

    token = _read_token()
    try:
        served = read_build(
            fetch_readyz(args.base_url, args.timeout, token), token_sent=token is not None
        )
    except CheckError:
        raise
    except Exception as exc:  # any fetch or parse failure: nothing was compared
        raise CheckError(f"could not read build info from readyz ({type(exc).__name__})") from None

    try:
        from menhir import __version__ as local_version
    except Exception as exc:
        raise CheckError(f"menhir is not importable here ({type(exc).__name__})") from None

    expected_version = (
        args.expect_version.strip() if args.expect_version is not None else local_version
    )
    results = [_compare("version", expected_version, served["version"])]
    if args.expect_commit is not None:
        results.append(
            _compare(
                "commit",
                args.expect_commit,
                served["commit"],
                ok=served["commit"].lower().startswith(args.expect_commit.lower()),
            )
        )

    unchecked: list[str] = []
    if expected_version != local_version:
        unchecked.append(
            f"mcp_metadata_sha256: not checked; the local package is {local_version}, not "
            f"{expected_version}, and the digest can only be checked from a matching local package"
        )
    elif not _DIGEST_RE.fullmatch(served["mcp_metadata_sha256"]):
        unchecked.append(
            "mcp_metadata_sha256: not checked; the server reports "
            f"{served['mcp_metadata_sha256']!r}"
        )
    else:
        try:
            mode, local = _local_metadata(args.snapshot_mode)
        except CheckError:
            raise
        except Exception as exc:
            raise CheckError(
                f"could not compute the local digest ({type(exc).__name__}: {exc})"
            ) from None
        print(
            f"local catalog: snapshot_mode={mode} tools={local.tool_count} "
            f"python={sys.version_info.major}.{sys.version_info.minor}"
        )
        ok = _compare("mcp_metadata_sha256", local.sha256, served["mcp_metadata_sha256"])
        results.append(ok)
        if not ok:
            print(
                f"hint: the local catalog was built with snapshot_mode={mode}; if the server "
                "runs another mode, rerun with --snapshot-mode <mode>"
            )

    if not all(results):
        for line in unchecked:
            print(line)
        return EXIT_MISMATCH
    if unchecked:
        raise CheckError("; ".join(unchecked))
    return EXIT_MATCH


if __name__ == "__main__":
    sys.exit(main())
