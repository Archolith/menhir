"""P2A staging receive tools: begin / chunk / status / abort. Off by default.

Plan: ``.agent/plans/menhir-mcp-snapshot-ingest-2026-09-16.md`` (P2A).

These exist so the transport measurement runs against the REAL handler -- the same middleware,
auth, request parser and telemetry a release would use -- rather than against a proxy for it. An
`add_memory` padded-body probe cannot substitute: its schema validation, decoding, telemetry and
allocation all differ from the chunk path, which is the thing being sized.

**Three gates, not one.** Defence in depth here is cheap and the failure it prevents is a receive
surface nobody meant to expose:

1. `MENHIR_SNAPSHOT_RECEIVE_MODE` defaults to ``off``, and while it is off these classes are never
   registered -- so they are neither advertised nor invocable.
2. Every endpoint re-checks the mode at call time. Registration state is a process-start fact and
   this is a runtime one; a stale registration or a test harness holding an instance must still be
   refused.
3. Operator tier. The mode being on is an operator's decision, and so is using it.

Whether these tools stay hidden from model-facing catalogs once P2B ships, or are advertised with
"use `menhir sync`" guidance, is the plan's open decision 4 and is deliberately not settled here.

**Telemetry carries no content.** `call_payload` is overridden on the chunk tool to emit the
upload id, index, byte count and digest only. The default would record the arguments -- which on
this tool is the base64 of someone's source file -- into every telemetry row, which is exactly
what plan invariant 5 forbids.
"""

from __future__ import annotations

import os
from typing import Any

from menhir.mcp.contracts import ToolScope
from menhir.mcp.service_access import get_mcp_session
from menhir.mcp.tools.base import BaseJsonTool
from menhir.snapshot.receive import ReceiveError, StagingReceiver, UploadRecord

__all__ = [
    "SNAPSHOT_STAGING_TOOLS",
    "AbortProjectSnapshotTool",
    "BeginProjectSnapshotTool",
    "GetProjectSnapshotStatusTool",
    "PutProjectSnapshotChunkTool",
    "snapshot_receive_mode",
    "staging_enabled",
]

SNAPSHOT_RECEIVE_MODE_ENV = "MENHIR_SNAPSHOT_RECEIVE_MODE"

#: P2A knows two modes. P2B introduces `receive`/`shadow`/`write` as a registered feature flag;
#: until then anything other than `staging` means off, so a typo fails closed.
MODE_OFF = "off"
MODE_STAGING = "staging"

_DISABLED_MESSAGE = (
    "snapshot staging receive is disabled. It is a transport-measurement surface (plan P2A), "
    f"enabled only by setting {SNAPSHOT_RECEIVE_MODE_ENV}=staging on an operator instance."
)


def snapshot_receive_mode() -> str:
    return os.getenv(SNAPSHOT_RECEIVE_MODE_ENV, MODE_OFF).strip().lower() or MODE_OFF


def staging_enabled() -> bool:
    return snapshot_receive_mode() == MODE_STAGING


def _staging_root():
    from menhir.infrastructure.paths import state_dir

    return state_dir() / "snapshot-staging"


def _receiver() -> StagingReceiver:
    """Built per call rather than cached.

    The receiver holds no connection and no in-memory state -- the record on disk is the
    authority (invariant 11) -- so a fresh instance is correct by construction and a cached one
    would be a second place for state to live.
    """
    return StagingReceiver(_staging_root())


def _principal() -> str:
    """The authenticated caller an upload is bound to (invariant 2).

    Derived from the session, never from an argument: a caller-supplied principal is a claim to
    be someone, which is the one thing ownership may not be built on.
    """
    session = get_mcp_session()
    return f"{session.user_id}"


def _record_json(record: UploadRecord) -> dict[str, Any]:
    """The client-facing view: progress and identifiers, no content and no internal paths."""
    return {
        "ok": True,
        "upload_id": record.upload_id,
        "state": record.state.value,
        "chunk_bytes": record.chunk_bytes,
        "total_chunks": record.total_chunks,
        "received_chunks": len(record.received),
        "received_bytes": record.received_bytes,
        "missing_chunks": record.missing[:64],
        "missing_count": len(record.missing),
        "failure_code": record.failure_code,
    }


class _StagingTool(BaseJsonTool):
    """Shared posture: operator tier, disabled unless the mode says otherwise."""

    oauth_scopes = ("menhir:admin",)
    required_tier = "operator"
    read_only_hint = False
    destructive_hint = False
    open_world_hint = False

    def _guard(self) -> None:
        if not staging_enabled():
            raise RuntimeError(_DISABLED_MESSAGE)

    def _refuse(self, exc: ReceiveError) -> str:
        return self.render_json(
            {"ok": False, "tool": self.operation, "error": {"code": exc.code, "message": str(exc)}}
        )


class BeginProjectSnapshotTool(_StagingTool):
    name = "begin_project_snapshot"
    scope = ToolScope.NAMESPACED
    title = "Begin Project Snapshot"
    description = (
        "Reserve a staging upload for a project snapshot bundle. Transport measurement only: "
        "nothing is extracted, scanned, or written to the graph. Models should not call this; "
        "it is driven by `menhir sync`."
    )

    def timeout_for(self, **_unused: object) -> int:
        return 30

    async def endpoint(
        self,
        project_key: str,
        declared_bytes: int,
        chunk_bytes: int | None = None,
        namespace: str = "",
    ) -> str:
        """Reserve an upload and return its id, negotiated chunk size, and chunk count.

        Args:
            project_key: Opaque key grouping uploads for one project; used for the per-project
                concurrency cap. Not a project identity and never an authorization.
            declared_bytes: Size of the archive the caller intends to send. A claim used for the
                chunk plan and an early refusal; what actually lands is counted chunk by chunk.
            chunk_bytes: Optional chunk size, bounded by the server's hard ceiling.
        """
        self._guard()
        try:
            record = _receiver().begin(
                principal=_principal(),
                project_key=project_key,
                declared_bytes=int(declared_bytes),
                chunk_bytes=int(chunk_bytes) if chunk_bytes else None,
            )
        except ReceiveError as exc:
            return self._refuse(exc)
        return self.render_json(_record_json(record))


class PutProjectSnapshotChunkTool(_StagingTool):
    name = "put_project_snapshot_chunk"
    scope = ToolScope.OBJECT
    title = "Put Project Snapshot Chunk"
    description = (
        "Upload one base64 chunk of a snapshot bundle. Models must not synthesize chunk "
        "content; this is driven by `menhir sync`."
    )

    def timeout_for(self, **_unused: object) -> int:
        return 60

    def call_payload(self, *_args: Any, **kwargs: Any) -> dict[str, Any]:
        """Telemetry payload: identifiers and sizes only (invariant 5).

        The default records the call's arguments, and one of this tool's arguments is the base64
        of a user's source file. That would put source content into every telemetry row, which is
        the single most expensive way to violate the invariant -- silently, at volume, in a store
        nobody reads until they need it.
        """
        return {
            "upload_id": kwargs.get("upload_id"),
            "index": kwargs.get("index"),
            "declared_len": kwargs.get("declared_len"),
            "digest": kwargs.get("digest"),
        }

    async def endpoint(
        self,
        upload_id: str,
        index: int,
        data_b64: str,
        declared_len: int,
        digest: str,
    ) -> str:
        """Accept one chunk at a zero-based index.

        Args:
            upload_id: The upload this chunk belongs to.
            index: Zero-based chunk index.
            data_b64: Standard base64 of the chunk's bytes.
            declared_len: Decoded length, checked before the decode and again after it.
            digest: SHA-256 of the decoded bytes, lowercase hex.
        """
        self._guard()
        try:
            record = _receiver().put_chunk(
                upload_id=upload_id,
                principal=_principal(),
                index=int(index),
                data_b64=data_b64,
                declared_len=int(declared_len),
                digest=digest,
            )
        except ReceiveError as exc:
            return self._refuse(exc)
        return self.render_json(_record_json(record))


class GetProjectSnapshotStatusTool(_StagingTool):
    name = "get_project_snapshot_status"
    scope = ToolScope.OBJECT
    title = "Get Project Snapshot Status"
    read_only_hint = True
    description = "Report an upload's state, progress, and which chunks are still missing."

    def timeout_for(self, **_unused: object) -> int:
        return 15

    async def endpoint(self, upload_id: str) -> str:
        """Return state, received/missing chunks, and any sanitized failure code."""
        self._guard()
        try:
            record = _receiver().status(upload_id=upload_id, principal=_principal())
        except ReceiveError as exc:
            return self._refuse(exc)
        return self.render_json(_record_json(record))


class AbortProjectSnapshotTool(_StagingTool):
    name = "abort_project_snapshot"
    scope = ToolScope.OBJECT
    title = "Abort Project Snapshot"
    destructive_hint = True
    description = "Cancel a staging upload and remove its staged bytes. Idempotent."

    def timeout_for(self, **_unused: object) -> int:
        return 15

    async def endpoint(self, upload_id: str) -> str:
        """Cancel the upload and drop its bytes. Repeating this is not an error."""
        self._guard()
        try:
            record = _receiver().abort(upload_id=upload_id, principal=_principal())
        except ReceiveError as exc:
            return self._refuse(exc)
        return self.render_json(_record_json(record))


#: Registered only while the mode is `staging` -- see `register_all_tools`.
SNAPSHOT_STAGING_TOOLS = [
    BeginProjectSnapshotTool,
    PutProjectSnapshotChunkTool,
    GetProjectSnapshotStatusTool,
    AbortProjectSnapshotTool,
]
