"""Operator snapshot tools: begin / chunk / explicit commit / status / abort. Off by default.

Plan: ``.agent/plans/menhir-mcp-snapshot-ingest-2026-09-16.md`` (P2A).

The P2A receive surface remains the transport boundary. Explicit commit now hands a sealed upload
to the mode-gated coordinator: RECEIVE records receipt, SHADOW extracts and scans, and WRITE may
publish. The final chunk never triggers processing implicitly.

**Three gates, not one.** Defence in depth here is cheap and the failure it prevents is a receive
surface nobody meant to expose:

1. `MENHIR_SNAPSHOT_RECEIVE_MODE` defaults to ``off``, and while it is off these classes are never
   registered -- so they are neither advertised nor invocable.
2. Every endpoint re-checks the mode at call time. Registration state is a process-start fact and
   this is a runtime one; a stale registration or a test harness holding an instance must still be
   refused.
3. Operator tier. The mode being on is an operator's decision, and so is using it.

**Owner decision (2026-09-16): these are advertised, not hidden.** Agents need to sync as part of
their workflow, so the descriptions guide rather than forbid: prefer `menhir sync`, call the tools
directly when it is not available, and never invent chunk bytes.

One consequence is deliberately left standing rather than quietly resolved: `required_tier` is
still `operator`, so a client holding only an agent key sees these tools and is refused when it
calls them. Advertising a tool the caller cannot use is its own kind of bad, and lowering the tier
is an authorization change rather than a visibility one -- it needs deciding on its own terms.

**Telemetry carries no content.** `call_payload` is overridden on the chunk tool to emit the
upload id, index, byte count and digest only. The default would record the arguments -- which on
this tool is the base64 of someone's source file -- into every telemetry row, which is exactly
what plan invariant 5 forbids.
"""

from __future__ import annotations

import asyncio
from typing import Any

from menhir.config.snapshot_mode import (
    SNAPSHOT_RECEIVE_MODE_ENV,
    SnapshotReceiveMode,
    snapshot_receive_mode,
)
from menhir.mcp.contracts import ToolScope
from menhir.mcp.service_access import get_mcp_session
from menhir.mcp.tools.base import BaseJsonTool
from menhir.snapshot.receive import ReceiveError, StagingReceiver, UploadRecord

__all__ = [
    "SNAPSHOT_STAGING_TOOLS",
    "AbortProjectSnapshotTool",
    "BeginProjectSnapshotTool",
    "CommitProjectSnapshotTool",
    "GetProjectSnapshotStatusTool",
    "PutProjectSnapshotChunkTool",
    "snapshot_receive_mode",
    "staging_enabled",
]

#: P2B: the mode is declared in `menhir.config.snapshot_mode`, not parsed here. This module used
#: to own the env read and compare it to a literal; that is exactly the shape that lets `receive`
#: drift into doing something `shadow` should. Re-exported so existing callers and tests keep
#: their import site.
MODE_OFF = SnapshotReceiveMode.OFF.value
MODE_STAGING = SnapshotReceiveMode.RECEIVE.value

_DISABLED_MESSAGE = (
    "snapshot receive is disabled. It is an operator surface (plan P2A/P2B), enabled only by "
    f"setting {SNAPSHOT_RECEIVE_MODE_ENV} to receive, shadow, or write on an operator instance."
)


def staging_enabled() -> bool:
    """Whether the receive tools may be registered and invoked.

    Asks the mode for the capability rather than comparing it to a name: every mode above OFF
    accepts uploads, and `shadow`/`write` must not have to be remembered here when P3 and P4
    introduce them.
    """
    return snapshot_receive_mode().accepts_uploads


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
        "project_id": record.project_id,
        "snapshot_id": record.snapshot_id,
        "result": record.result,
    }


class _StagingTool(BaseJsonTool):
    """Shared posture: operator tier, disabled unless the mode says otherwise."""

    oauth_scopes = ("menhir:admin",)
    required_tier = "operator"
    read_only_hint = False
    destructive_hint = False
    open_world_hint = False

    def _guard(self) -> SnapshotReceiveMode:
        mode = snapshot_receive_mode()
        if not mode.accepts_uploads:
            raise RuntimeError(_DISABLED_MESSAGE)
        return mode

    def _refuse(self, exc: ReceiveError) -> str:
        return self.render_json(
            {"ok": False, "tool": self.operation, "error": {"code": exc.code, "message": str(exc)}}
        )


class BeginProjectSnapshotTool(_StagingTool):
    name = "begin_project_snapshot"
    scope = ToolScope.NAMESPACED
    title = "Begin Project Snapshot"
    description = (
        "Reserve an upload for a project snapshot bundle. Returns the upload id and the chunk "
        "size to use -- send chunks at THAT size, not a size of your own choosing. The snapshot "
        "remains inert until `commit_project_snapshot` is called. Prefer running `menhir sync`, which builds the "
        "bundle and drives this; call these tools directly only when that is not available."
    )

    def timeout_for(self, **_unused: object) -> int:
        return 30

    async def endpoint(
        self,
        project_key: str,
        declared_bytes: int,
        chunk_bytes: int | None = None,
        project_id: str | None = None,
        namespace: str = "",
    ) -> str:
        """Reserve an upload and return its id, negotiated chunk size, and chunk count.

        Args:
            project_key: Opaque key grouping uploads for one project; used for the per-project
                concurrency cap. Not a project identity and never an authorization.
            declared_bytes: Size of the archive the caller intends to send. A claim used for the
                chunk plan and an early refusal; what actually lands is counted chunk by chunk.
            chunk_bytes: Optional chunk size, bounded by the server's hard ceiling.
            project_id: A prior server-issued project receipt, if this checkout has one.
        """
        self._guard()
        try:
            record = _receiver().begin(
                principal=_principal(),
                project_key=project_key,
                declared_bytes=int(declared_bytes),
                chunk_bytes=int(chunk_bytes) if chunk_bytes else None,
                project_id=project_id,
            )
        except ReceiveError as exc:
            return self._refuse(exc)
        payload = _record_json(record)
        # Capability handshake for clients that must know explicit commit exists before sending
        # source bytes. Pre-commit servers omit this field and are refused client-side.
        payload["commit_required"] = True
        return self.render_json(payload)


class PutProjectSnapshotChunkTool(_StagingTool):
    name = "put_project_snapshot_chunk"
    scope = ToolScope.OBJECT
    title = "Put Project Snapshot Chunk"
    description = (
        "Upload one base64 chunk of a snapshot bundle, at the chunk size `begin_project_snapshot` "
        "returned and at its zero-based index. **Never synthesize chunk content.** Every byte must "
        "come from a bundle built by the bundler: invented bytes produce a snapshot that does not "
        "correspond to any real repository, and the server cannot tell the difference."
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


class CommitProjectSnapshotTool(_StagingTool):
    name = "commit_project_snapshot"
    scope = ToolScope.OBJECT
    title = "Commit Project Snapshot"
    description = (
        "Explicitly commit a completely uploaded snapshot. In receive mode this records receipt; "
        "in shadow mode it extracts and scans without graph writes; in write mode it atomically "
        "publishes the scanned structure. Returns the durable terminal server stage."
    )

    def timeout_for(self, **_unused: object) -> int:
        return 900

    async def endpoint(self, upload_id: str) -> str:
        """Process a sealed upload under the configured receive-mode capability."""
        mode = self._guard()
        principal = _principal()
        receiver = _receiver()
        neo4j = None
        if mode.writes_graph:
            from menhir.core.runtime import _state

            if _state.built is None:
                return self.render_json(
                    {
                        "ok": False,
                        "tool": self.operation,
                        "error": {
                            "code": "snapshot.pipeline.runtime_unavailable",
                            "message": "snapshot write runtime is not ready",
                        },
                    }
                )
            neo4j = _state.built.graph_adapter.neo4j

        from menhir.infrastructure.paths import state_dir
        from menhir.snapshot.coordinator import SnapshotCoordinator

        coordinator = SnapshotCoordinator(
            receiver, state_root=state_dir(), mode=mode, neo4j=neo4j
        )
        try:
            record = await asyncio.to_thread(
                coordinator.commit, upload_id=upload_id, principal=principal
            )
        except ReceiveError as exc:
            return self._refuse(exc)
        except Exception as exc:  # noqa: BLE001 -- expose a code, never attacker-derived details
            return self.render_json(
                {
                    "ok": False,
                    "tool": self.operation,
                    "error": {
                        "code": str(getattr(exc, "code", "snapshot.pipeline.failed")),
                        "message": "snapshot processing failed",
                    },
                }
            )
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


#: Registered only while the configured mode accepts uploads -- see `register_all_tools`.
SNAPSHOT_STAGING_TOOLS = [
    BeginProjectSnapshotTool,
    PutProjectSnapshotChunkTool,
    CommitProjectSnapshotTool,
    GetProjectSnapshotStatusTool,
    AbortProjectSnapshotTool,
]
