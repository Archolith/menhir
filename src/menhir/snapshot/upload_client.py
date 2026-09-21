"""Client half of the snapshot receive protocol: send a bundle to a remote Menhir.

Plan: ``.agent/plans/menhir-mcp-snapshot-ingest-2026-09-16.md`` (P2B).

Speaks MCP over HTTP directly to the remote server's snapshot tools. Deliberately NOT routed
through `BackendClient`: that talks the internal backend protocol at `/api/internal/backend/...`,
which does not forward MCP tool calls, so a deployment fronted by a backend-first proxy cannot
carry an upload. The MVP requires a direct remote MCP endpoint and says so plainly when it does not
have one, rather than failing with a 404 the user has to interpret.

**The server's chunk plan wins.** `begin` returns the chunk size the server negotiated, and every
chunk is cut to THAT, never to the local `SnapshotLimits` default. The server seeks to
``index * chunk_bytes`` when placing bytes, so a client that used its own size would write every
chunk at the wrong offset -- and each chunk's digest would still verify, because a chunk is
individually intact wherever it lands. The result is a bundle that passes every per-chunk check
and is silently wrong.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from menhir.snapshot.protocol import sha256_hex

__all__ = [
    "SnapshotUploadError",
    "SnapshotUploader",
    "UploadOutcome",
]

#: Sent so the request is not refused by an edge before it reaches the origin. A Cloudflare zone
#: with Browser Integrity Check on rejects urllib's default signature, and a request with no agent
#: at all, with a 403 the origin never sees.
_USER_AGENT = "menhir-sync"

#: The receiver's code for a rejected declared size OR chunk size. Named here rather than imported
#: from `menhir.snapshot.receive`: that module is the SERVER half, and a client that imports it
#: would stop being a client -- the point of speaking a wire protocol is that the two halves can
#: ship separately. The string is part of the frozen contract.
_ERR_SIZE_REJECTED = "snapshot.upload.declared_size_rejected"


class SnapshotUploadError(RuntimeError):
    """A refusal or transport failure, carrying the server's stable code where there is one."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class UploadOutcome:
    upload_id: str
    state: str
    chunk_bytes: int
    total_chunks: int
    sent_chunks: int
    bytes_sent: int
    project_id: str
    snapshot_id: str
    result: dict[str, Any]


class SnapshotUploader:
    """Drives begin -> chunk* -> sealed status -> explicit commit against remote Menhir."""

    def __init__(
        self,
        base_url: str,
        *,
        auth_key: str,
        timeout_s: float = 120.0,
        user_agent: str = _USER_AGENT,
    ) -> None:
        self.endpoint = f"{base_url.rstrip('/')}/mcp-http"
        self.auth_key = auth_key
        self.timeout_s = timeout_s
        self.user_agent = user_agent

    # -- transport ------------------------------------------------------------------------------

    def _call(
        self, tool: str, arguments: dict[str, Any], *, timeout_s: float | None = None
    ) -> dict[str, Any]:
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        }
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "Authorization": f"Bearer {self.auth_key}",
                "User-Agent": self.user_agent,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout_s if timeout_s is None else timeout_s
            ) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raise SnapshotUploadError(
                "sync.transport.http_error",
                f"the server answered HTTP {exc.code} at {self.endpoint}",
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise SnapshotUploadError(
                "sync.transport.unreachable", f"could not reach {self.endpoint}: {exc}"
            ) from exc

        parsed = _parse_mcp_body(raw)
        payload = _tool_payload(parsed)

        # "No such tool" arrives two different ways and both mean the same thing to a user. A
        # JSON-RPC `error` is the obvious one; the MCP server also answers with a normal result
        # carrying `isError` and a plain-text body, which is NOT JSON and would otherwise be
        # reported as an unreadable reply -- the least useful message available for the single
        # most likely misconfiguration.
        result = parsed.get("result")
        is_error_result = isinstance(result, dict) and bool(result.get("isError"))
        if "error" in parsed or is_error_result:
            detail = (
                str((parsed.get("error") or {}).get("message", "")).strip()
                if "error" in parsed
                else (payload if isinstance(payload, str) else "")
            ).strip()
            raise SnapshotUploadError(
                "sync.server.tool_unavailable",
                f"{self.endpoint} did not accept the `{tool}` tool ({detail or 'no detail'}). "
                "That server may have snapshot receive disabled, may be older than the snapshot "
                "tools, or may be a backend-first proxy, which cannot carry an upload.",
            )

        if not isinstance(payload, dict):
            raise SnapshotUploadError(
                "sync.server.unreadable_reply", f"could not read the reply to `{tool}`"
            )
        if payload.get("ok") is False:
            error = payload.get("error") or {}
            raise SnapshotUploadError(
                str(error.get("code") or "sync.server.refused"),
                str(error.get("message") or f"the server refused `{tool}`"),
            )
        return payload

    # -- the upload -----------------------------------------------------------------------------

    def upload(
        self,
        archive: Path,
        *,
        project_key: str,
        project_id: str | None = None,
        chunk_bytes: int | None = None,
        progress: Callable[[int, int], None] | None = None,
    ) -> UploadOutcome:
        """Send `archive` and return where it ended up.

        `chunk_bytes` is a REQUEST. What the server returns from `begin` is what is used, because
        the server places bytes by `index * chunk_bytes` and only it knows its own ceiling.
        """
        size = archive.stat().st_size
        try:
            begun = self._call(
                "begin_project_snapshot",
                {
                    "project_key": project_key,
                    "project_id": project_id,
                    "declared_bytes": size,
                    "chunk_bytes": chunk_bytes,
                },
            )
        except SnapshotUploadError as exc:
            # The server REFUSES a chunk size above its ceiling rather than clamping it, so a
            # client that guesses high is turned away for a preference it does not need to hold.
            # Retry once with no request at all and take the server's default. Only the size
            # refusal is retried: any other code is a real refusal and must stand.
            if chunk_bytes is None or exc.code != _ERR_SIZE_REJECTED:
                raise
            begun = self._call(
                "begin_project_snapshot",
                {
                    "project_key": project_key,
                    "project_id": project_id,
                    "declared_bytes": size,
                    "chunk_bytes": None,
                },
            )
        upload_id = str(begun.get("upload_id") or "")
        negotiated = int(begun.get("chunk_bytes") or 0)
        total_chunks = int(begun.get("total_chunks") or 0)
        if not upload_id or negotiated <= 0:
            raise SnapshotUploadError(
                "sync.server.unreadable_reply", "the server did not return a usable chunk plan"
            )
        if begun.get("commit_required") is not True:
            try:
                self._call("abort_project_snapshot", {"upload_id": upload_id})
            except SnapshotUploadError:
                pass
            raise SnapshotUploadError(
                "sync.server.commit_unavailable",
                "the server does not advertise explicit snapshot commit support; no source bytes "
                "were sent. Upgrade the remote Menhir before syncing.",
            )

        sent = 0
        bytes_sent = 0
        try:
            with archive.open("rb") as handle:
                for index in range(total_chunks):
                    # Read sequentially rather than seeking: the file is ours, the chunk size is
                    # fixed, and a seek per chunk would invite the same offset mistake on this
                    # side that using a local chunk size would.
                    data = handle.read(negotiated)
                    if not data:
                        break
                    self._call(
                        "put_project_snapshot_chunk",
                        {
                            "upload_id": upload_id,
                            "index": index,
                            "data_b64": base64.b64encode(data).decode("ascii"),
                            "declared_len": len(data),
                            "digest": sha256_hex(data),
                        },
                    )
                    sent += 1
                    bytes_sent += len(data)
                    if progress is not None:
                        progress(sent, total_chunks)
        except SnapshotUploadError:
            # Give the slot back rather than leaving it to the inactivity TTL. Best effort: the
            # original failure is the one worth reporting, and an abort that also fails must not
            # replace it.
            try:
                self._call("abort_project_snapshot", {"upload_id": upload_id})
            except SnapshotUploadError:
                pass
            raise

        staged = self._call("get_project_snapshot_status", {"upload_id": upload_id})
        if str(staged.get("state") or "") != "SEALED":
            raise SnapshotUploadError(
                "sync.server.incomplete_upload",
                "the server did not seal the upload after accepting its chunks",
            )
        final = self._call(
            "commit_project_snapshot", {"upload_id": upload_id}, timeout_s=900.0
        )
        return UploadOutcome(
            upload_id=upload_id,
            state=str(final.get("state") or "UNKNOWN"),
            chunk_bytes=negotiated,
            total_chunks=total_chunks,
            sent_chunks=sent,
            bytes_sent=bytes_sent,
            project_id=str(final.get("project_id") or ""),
            snapshot_id=str(final.get("snapshot_id") or ""),
            result=dict(final.get("result") or {}),
        )


def _parse_mcp_body(body: str) -> dict[str, Any]:
    """Accept a plain JSON body or an SSE frame; the transport may use either."""
    text = body.strip()
    if text.startswith(("event:", "data:")):
        for line in text.splitlines():
            if line.startswith("data:"):
                text = line[len("data:") :].strip()
                break
    if not text:
        raise SnapshotUploadError("sync.server.unreadable_reply", "the server sent an empty reply")
    try:
        return json.loads(text)
    except ValueError as exc:
        raise SnapshotUploadError(
            "sync.server.unreadable_reply", "the server sent a reply that is not JSON"
        ) from exc


def _tool_payload(parsed: dict[str, Any]) -> Any:
    """Unwrap the tool's JSON out of the MCP envelope, tolerating either shape."""
    result = parsed.get("result")
    if not isinstance(result, dict):
        return parsed
    content = result.get("content")
    if isinstance(content, list) and content and isinstance(content[0], dict):
        inner = content[0].get("text")
        if isinstance(inner, str):
            try:
                return json.loads(inner)
            except ValueError:
                return inner
    return result
