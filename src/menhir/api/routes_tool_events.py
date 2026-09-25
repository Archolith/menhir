"""Tool-event ingestion endpoint (Hook Center v0)."""

from __future__ import annotations

import asyncio
import logging

from fastapi import HTTPException, Request

from .routes_router import router
from .routes_support import ToolEventRequest, ToolEventResponse, _require_runtime_context, _require_tier

logger = logging.getLogger(__name__)


@router.post("/tool-events", response_model=ToolEventResponse)
async def record_tool_event(request: Request, body: ToolEventRequest) -> ToolEventResponse:
    """Hook Center v0: accept a normalized tool/file event and DETERMINISTICALLY mark the affected
    structure-file node dirty (so stale file references become detectable). No file content, no
    transcript, no LLM, no structure rebuild. Forward-compatible name: v0 handles `file_changed`;
    other `event_type`s are accepted-and-ignored so future tool events don't break older servers."""
    _require_tier("agent")
    runtime_ctx = _require_runtime_context(request)
    if body.event_type != "file_changed":
        # accept-and-ignore: keeps the endpoint forward-compatible for later tool-event kinds.
        return ToolEventResponse(accepted=True, event_type=body.event_type, operation=body.operation,
                                 matched=0, marked_dirty=False, ignored_reason="unsupported event_type in v0")
    path = (body.path or "").strip()
    if not path:
        raise HTTPException(status_code=400, detail="path is required for a file_changed event")
    adapter = getattr(runtime_ctx.built, "graph_adapter", None)
    if adapter is None or not hasattr(adapter, "record_file_event"):
        raise HTTPException(status_code=503, detail="tool-event capture unavailable")
    # Derive only the structural scope from project_root. Artifact repository identity is separate.
    project = body.project
    if project is None and body.project_root:
        import os as _os
        project = _os.path.basename(body.project_root.rstrip("/\\")) or None
    try:
        result = await asyncio.to_thread(
            adapter.record_file_event,
            path=path,
            operation=body.operation,
            old_path=body.old_path,
            project=project,
            after_hash=body.after_hash,
            mtime=body.mtime,
            source_client=body.source_client,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Artifact source reconciliation is a second, independent consumer of the
    # same event. It runs after the structural mark has already been recorded so
    # a reconciliation refusal cannot roll back or suppress stale detection --
    # the hook is a low-latency accelerator, not the coverage backstop.
    reconciliation = None
    if hasattr(adapter, "reconcile_file_event_source"):
        repository = (body.repository or "").strip()
        if not repository:
            reconciliation = {
                "attempted": True,
                "applied": False,
                "reason": "repository_identity_missing",
            }
        else:
            try:
                reconciliation = await asyncio.to_thread(
                    adapter.reconcile_file_event_source,
                    path=path,
                    operation=body.operation,
                    old_path=body.old_path,
                    repository=repository,
                    after_hash=body.after_hash,
                    git_commit=body.git_commit,
                )
            except Exception as exc:  # noqa: BLE001 - never fail the hook on this leg
                logger.warning("artifact source reconciliation failed for %s: %s", path, exc)
                reconciliation = {"attempted": True, "applied": False, "reason": "error"}
            if reconciliation and not reconciliation.get("attempted"):
                reconciliation = None  # not corpus material; nothing worth reporting

    return ToolEventResponse(
        accepted=True, event_type=body.event_type, operation=body.operation,
        matched=int(result.get("matched", 0)), marked_dirty=bool(result.get("marked_dirty", False)),
        artifact_reconciliation=reconciliation,
    )
