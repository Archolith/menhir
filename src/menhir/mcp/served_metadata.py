"""Build metadata a deploy check can compare against the running server.

The digest covers what MCP clients cache at connect time: the server instructions and the
unfiltered tool catalog (name, description, input schema). It is diagnostic only.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re

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


def _get_lock() -> asyncio.Lock:
    global _digest_lock, _digest_lock_loop
    loop = asyncio.get_running_loop()
    if _digest_lock is None or _digest_lock_loop is not loop:
        _digest_lock = asyncio.Lock()
        _digest_lock_loop = loop
    return _digest_lock


async def _compute_digest() -> str:
    from mcp.server.fastmcp import FastMCP

    from menhir.api.mcp_remote import create_remote_tool_only_mcp
    from menhir.mcp import instructions

    server = create_remote_tool_only_mcp()
    # Base-class call: bypasses the tier/client filtering override so the result
    # does not depend on request context.
    tools = await FastMCP.list_tools(server)
    document = {
        "instructions": instructions.SERVER_INSTRUCTIONS,
        "tools": sorted(
            (
                {
                    "name": tool.name,
                    "description": tool.description,
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
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def served_mcp_metadata_digest() -> str:
    """Return the sha256 hex digest of served MCP instructions and tool catalog."""

    global _digest_cache
    if _digest_cache is not None:
        return _digest_cache
    async with _get_lock():
        if _digest_cache is None:
            _digest_cache = await _compute_digest()
        return _digest_cache
