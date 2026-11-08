"""Build metadata a deploy check can compare against the running server.

The digest covers the server instructions and the unfiltered tool catalog (name, description,
input schema) that MCP clients read at connect time. It is diagnostic only.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from dataclasses import dataclass
from typing import Any

_COMMIT_RE = re.compile(r"[0-9a-f]{7,40}")

_digest_cache: str | None = None
_digest_lock: asyncio.Lock | None = None
_digest_lock_loop: asyncio.AbstractEventLoop | None = None


def _reset_digest_cache() -> None:
    """Forget the cached digest (tests only)."""

    global _digest_cache, _digest_lock, _digest_lock_loop
    _digest_cache = None
    _digest_lock = None
    _digest_lock_loop = None


def release_commit() -> str:
    """Return the build commit from ``MENHIR_RELEASE_COMMIT`` or ``"unknown"``."""

    value = os.environ.get("MENHIR_RELEASE_COMMIT", "").strip()
    return value if _COMMIT_RE.fullmatch(value) else "unknown"


def served_server_version(version: str) -> str:
    """MCP ``serverInfo.version``: ``version``, plus the build commit as SemVer build metadata.

    ``1.2.3+43e693f6b821`` still parses as version 1.2.3 (SemVer and PEP 440 both ignore the
    ``+`` part when comparing), and it names the build a long-lived connection was opened
    against (#498). Without a known commit the version is unchanged.
    """

    commit = release_commit()
    return version if commit == "unknown" else f"{version}+{commit[:12]}"


def _get_lock() -> asyncio.Lock:
    global _digest_lock, _digest_lock_loop
    loop = asyncio.get_running_loop()
    if _digest_lock is None or _digest_lock_loop is not loop:
        _digest_lock = asyncio.Lock()
        _digest_lock_loop = loop
    return _digest_lock


@dataclass(frozen=True)
class MetadataDigest:
    """A served-metadata digest and the number of tools it covers."""

    sha256: str
    tool_count: int


def _normalized_description(description: str | None) -> str | None:
    """Description text as hashed: each line stripped of surrounding whitespace.

    CPython 3.13 strips docstring indentation at compile time and 3.12 does not, so the same
    source serves differently indented descriptions. ``inspect.cleandoc`` cannot undo that: a
    served description is the curated line plus the stripped docstring, so one of its lines is
    never indented and the common margin is zero. Stripping per line keeps the digest equal
    across interpreter versions; the served text is not changed.
    """

    if description is None:
        return None
    return "\n".join(line.strip() for line in description.splitlines())


async def compute_metadata(server: Any = None) -> MetadataDigest:
    """Digest *server*'s instructions and unfiltered tool catalog. Not cached.

    With no server, builds the remote MCP surface the HTTP app serves.
    """

    from mcp.server.fastmcp import FastMCP

    if server is None:
        from menhir.api.mcp_remote import create_remote_tool_only_mcp

        server = create_remote_tool_only_mcp()
    # Base-class call: bypasses the tier/client filtering override so the result
    # does not depend on request context.
    tools = await FastMCP.list_tools(server)
    document = {
        "instructions": server.instructions,
        "tools": sorted(
            (
                {
                    "name": tool.name,
                    "description": _normalized_description(tool.description),
                    "inputSchema": tool.inputSchema,
                }
                for tool in tools
            ),
            key=lambda item: item["name"],
        ),
    }
    canonical = json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return MetadataDigest(
        sha256=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        tool_count=len(tools),
    )


async def _compute_digest(server: Any = None) -> str:
    return (await compute_metadata(server)).sha256


RELEASE_RECORD_SCHEMA = 1


def release_record() -> dict[str, Any]:
    """What a release publishes for a deploy to compare with ``/readyz`` (#498).

    Run inside the release image, so the commit and catalog are the ones it serves. The catalog
    depends on the snapshot receive mode, which the release cannot know, so every mode's digest
    is listed; a deploy accepts the served digest when it equals one of them.
    """

    from menhir import __version__
    from menhir.config.snapshot_mode import SNAPSHOT_RECEIVE_MODE_ENV, SnapshotReceiveMode

    # Saved for the restore below, not read as configuration: each pass sets the mode itself.
    previous = os.environ.pop(SNAPSHOT_RECEIVE_MODE_ENV, None)
    digests: dict[str, str] = {}
    try:
        for mode in SnapshotReceiveMode:
            os.environ[SNAPSHOT_RECEIVE_MODE_ENV] = mode.value
            digests[mode.value] = asyncio.run(compute_metadata()).sha256
    finally:
        if previous is None:
            os.environ.pop(SNAPSHOT_RECEIVE_MODE_ENV, None)
        else:
            os.environ[SNAPSHOT_RECEIVE_MODE_ENV] = previous
    return {
        "schema": RELEASE_RECORD_SCHEMA,
        "version": __version__,
        "commit": release_commit(),
        "mcp_metadata_sha256": digests,
    }


def main(argv: list[str] | None = None) -> int:
    """``python -m menhir.mcp.served_metadata``: print the release record as one JSON line."""

    import argparse

    argparse.ArgumentParser(description=release_record.__doc__.splitlines()[0]).parse_args(argv)
    print(json.dumps(release_record(), sort_keys=True))
    return 0


async def served_mcp_metadata_digest(server: Any = None) -> str:
    """Return the sha256 hex digest of served MCP instructions and tool catalog.

    Pass the FastMCP instance the process serves when there is one; without it a twin is built.
    The first successful call computes the digest and caches it for the process.
    """

    global _digest_cache
    if _digest_cache is not None:
        return _digest_cache
    async with _get_lock():
        if _digest_cache is None:
            _digest_cache = await (
                _compute_digest() if server is None else _compute_digest(server)
            )
        return _digest_cache


if __name__ == "__main__":
    raise SystemExit(main())
