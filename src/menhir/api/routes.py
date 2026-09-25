"""REST API endpoints for remote memory access.

Cohesive route groups live in the sibling ``routes_*.py`` modules; all of them decorate the
shared router from ``routes_router``. This module stays the facade every existing import site
uses (``menhir.api.routes``) and hosts the routes whose module-level guard seams and
source-level checks (``inspect.getsource`` / AST allowlists) pin them here.
"""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import HTTPException, Query, Request

from menhir.domain.bootstrap_scope import bootstrap_selection
from menhir.domain.recall import InvalidQueryPresetError
from menhir.domain.session import new_session
from menhir.domain.structural_memory import is_structural_memory_row

# Facade re-exports: every moved public symbol is re-exported here, so existing
# `from menhir.api.routes import X` sites keep working. The routes_support guards
# (_get_backend, _require_tier, ...) stay bound here because the handlers below read
# them from this namespace and tests patch them on this module.
from .routes_admin import list_clients, mint_client, revoke_client
from .routes_handlers import phase3_reset_impl
from .routes_health import health, ready
from .routes_ingest_support import _count_linked_entities, _terminal_status_from_row
from .routes_internal import backend_invoke
from .routes_phase3 import phase3_run, phase3_status, phase3_views
from .routes_router import router
from .routes_scalar_authority import scalar_authority_contributors
from .routes_support import (
    BootstrapContextRequest, ContextRequest, ContextResponse,
    EpisodeAdmissionRequest, EpisodeAdmissionResponse,
    FlagResponse, MemoryRequest, MemoryResponse, Phase3ResetResponse,
    RecallMemory, RecallRequest, RecallResponse,
    StaleAnchorVerificationRequest, StaleAnchorVerificationResponse,
    StatsResponse, UnflagResponse,
    _BACKEND_METHODS, _OP_TIER_AGENT, _OP_TIER_OPERATOR,
    _capability_payload, _get_backend, _get_runtime_context,
    _require_phase3_adapter, _require_runtime_context, _require_tier,
    _required_tier_for_operation, _resolve_caller_session, _resolve_namespace,
    _service_payload, _try_record_destructive_op_rest,
)
from .routes_tool_events import record_tool_event
from .routes_turn_evidence import link_episode_admission, record_turn_evidence


@router.post("/recall", response_model=RecallResponse, response_model_exclude_none=True)
async def recall(request: Request, body: RecallRequest) -> RecallResponse:
    backend = _get_backend(request)
    recall_kwargs: dict[str, object] = {}
    if body.trace:
        recall_kwargs["trace"] = True
    try:
        result = await backend.recall(
            body.query,
            preset=body.preset,
            limit=body.limit,
            include_session=body.include_session,
            include_superseded=body.include_superseded,
            include_invalidated=body.include_invalidated,
            namespace=_resolve_namespace(request, body.namespace),
            **recall_kwargs,
        )
    except InvalidQueryPresetError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return RecallResponse(
        query=str(result.get("query") or body.query),
        preset=str(result.get("preset") or body.preset),
        results=[
            RecallMemory(
                uuid=str(m.get("uuid") or ""),
                name=str(m.get("name") or ""),
                content=m.get("content"),
                scope=str(m.get("scope") or ""),
                memory_type=str(m.get("memory_type") or ""),
                final_score=float(m.get("final_score") or 0.0),
                retrieval_score=(
                    float(m["retrieval_score"])
                    if m.get("retrieval_score") is not None
                    else None
                ),
                retrieval_score_kind=str(
                    getattr(m.get("retrieval_score_kind"), "value", None)
                    or m.get("retrieval_score_kind")
                    or "graphiti_rrf"
                ),
                is_superseded_view=bool(m.get("is_superseded_view")),
                is_scalar_authority=bool(m.get("is_scalar_authority")),
                temporal_facts=m.get("temporal_facts") or [],
            )
            for m in result.get("results", []) or []
        ],
        candidates_evaluated=int(result.get("candidates_evaluated") or 0),
        trace=result.get("trace"),
        authority_layer=result.get("authority_layer"),
        event_authority_layer=result.get("event_authority_layer"),
    )


@router.get("/bootstrap/flagged")
async def bootstrap_flagged(
    request: Request,
    reader_id: str = "default",
    workspace: str | None = None,
    limit: int = Query(default=10, ge=1, le=50),
) -> dict[str, object]:
    """Read scoped pins and issue a reader-local bootstrap receipt."""
    from menhir.mcp.formatters import _normalize_reader_id
    from menhir.mcp.lifecycle import _remember_flagged_bootstrap_read

    backend = _get_backend(request)
    normalized_reader = _normalize_reader_id(reader_id)
    selection_key, _allowed = bootstrap_selection(workspace)
    # Resolved server-side, like every other REST read of memory content. This route took no
    # namespace at all, so a pinned client's bootstrap pins were drawn from every silo --
    # and `workspace` is not a substitute: it selects a bootstrap SCOPE, not a tenant.
    #
    # The version hash takes the same namespace as the rows. If it did not, two clients pinned
    # to different silos would receive different pins under the SAME version string, and the
    # receipt in `_remember_flagged_bootstrap_read` would report a client as freshly
    # bootstrapped on content it never saw.
    resolved_namespace = _resolve_namespace(request, None)
    rows = await backend.fetch_flagged_memories(
        limit=limit, workspace=workspace, namespace=resolved_namespace
    )
    rows = [row for row in rows if not is_structural_memory_row(row)]
    version = await backend.fetch_flagged_memory_bootstrap_version(
        workspace=workspace, namespace=resolved_namespace
    )
    # CF-238: the receipt is keyed on the RAW workspace and the RESOLVED namespace, separately.
    # `/bootstrap/context` must build its key from the same two values or the handshake can
    # never match.
    _remember_flagged_bootstrap_read(
        normalized_reader, version, workspace=workspace, namespace=resolved_namespace
    )
    return {
        "reader_id": normalized_reader,
        "bootstrap_selection": selection_key,
        "bootstrap_ready": True,
        "flagged_version": version,
        "flagged_count": len(rows),
        "items": rows,
    }


@router.post("/bootstrap/context")
async def bootstrap_context(
    request: Request, body: BootstrapContextRequest
) -> dict[str, object]:
    """Return scoped recent/relevant context after the matching pin receipt."""
    from menhir.mcp.formatters import _compact_scored_item, _normalize_reader_id
    from menhir.mcp.lifecycle import _has_recent_flagged_bootstrap_read
    from types import SimpleNamespace

    backend = _get_backend(request)
    normalized_reader = _normalize_reader_id(body.reader_id)
    # Resolved, not read raw off the body: this route reads memory content and must honour the
    # server-side namespace pin like every other REST read. It previously used body.namespace
    # directly and so was exempt from it.
    resolved_namespace = _resolve_namespace(request, body.namespace)
    # CF-238. Two different tenants are in play here and conflating them is the defect.
    #
    # `resolved_namespace` above scopes the CONTENT this route reads, and honours `body.namespace`.
    # The RECEIPT's tenant is a different thing: it must be whatever `/bootstrap/flagged` used, and
    # that route takes no namespace argument at all -- it resolves server-side from the pin or the
    # header only. Passing `body.namespace` in here made the check side ask about a tenant the
    # record side could never have written.
    #
    # This route also used to fold the namespace into the workspace
    # (`body.workspace or resolved_namespace`) and compute the version with NO namespace, while the
    # record side keyed on the raw workspace and versioned WITH the namespace -- so both halves of
    # the comparison diverged and a pinned client that omitted `workspace` could never clear the
    # gate. Both halves are now derived exactly as the record side derives them.
    resolved_receipt_namespace = _resolve_namespace(request, None)
    selection_key, _allowed = bootstrap_selection(body.workspace)
    version = await backend.fetch_flagged_memory_bootstrap_version(
        workspace=body.workspace, namespace=resolved_receipt_namespace
    )
    if not _has_recent_flagged_bootstrap_read(
        normalized_reader, version, workspace=body.workspace, namespace=resolved_receipt_namespace
    ):
        raise HTTPException(
            status_code=409,
            detail="bootstrap receipt required for the current workspace pin version",
        )

    relevant: list[dict[str, object]] = []
    if body.query.strip():
        recalled = await backend.recall(
            body.query.strip(),
            limit=body.limit,
            include_session=True,
            namespace=resolved_namespace,
        )
        relevant = [
            _compact_scored_item(SimpleNamespace(**row))
            for row in recalled.get("results", [])
        ]

    recent_rows = await backend.fetch_recent_memories(
        limit=body.recent_limit * 3,
        namespace=resolved_namespace,
    )
    recent = [
        row
        for row in recent_rows
        if not bool(row.get("user_flagged")) and not is_structural_memory_row(row)
    ][: body.recent_limit]
    return {
        "reader_id": normalized_reader,
        "bootstrap_selection": selection_key,
        "bootstrap_verified": True,
        "flagged_version": version,
        "relevant": relevant,
        "recent": recent,
    }


@router.post("/context", response_model=ContextResponse)
async def context(request: Request, body: ContextRequest) -> ContextResponse:
    backend = _get_backend(request)
    try:
        result = await backend.build_context(
            body.query,
            max_tokens=body.max_tokens,
            preset=body.preset,
            include_scores=body.include_scores,
            namespace=_resolve_namespace(request, body.namespace),
        )
    except InvalidQueryPresetError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return ContextResponse(
        query=str(result.get("query") or body.query),
        context=str(result.get("context") or ""),
        token_estimate=int(result.get("token_estimate") or 0),
        memory_count=int(result.get("memory_count") or 0),
        truncated=bool(result.get("truncated")),
        preset=str(result.get("preset") or body.preset),
    )


@router.post("/memory", response_model=MemoryResponse)
async def ingest_memory(
    request: Request,
    body: MemoryRequest,
    wait: Annotated[bool, Query(description="Wait for enrichment to complete")] = False,
) -> MemoryResponse:
    _require_tier("agent")
    runtime_ctx = _require_runtime_context(request)
    backend = _get_backend(request)
    caller_session = _resolve_caller_session(request)
    if body.user_id is not None:
        session = new_session(body.user_id, session_id=body.session_id)
    elif body.session_id is not None:
        session = new_session(caller_session.user_id, session_id=body.session_id)
    else:
        session = caller_session
    ingest_kwargs: dict[str, object] = {}
    if body.flagged:
        ingest_kwargs["flagged"] = True
    if body.bootstrap_scope is not None:
        ingest_kwargs["bootstrap_scope"] = body.bootstrap_scope
    result = await backend.queue_episode(
        body.episode,
        user_id=session.user_id,
        session_id=session.session_id,
        source=body.source,
        diff=body.diff,
        namespace=_resolve_namespace(request, body.namespace),
        occurred_at=body.occurred_at,
        turn_evidence_uuid=body.turn_evidence_uuid,
        **ingest_kwargs,
    )
    status = str(result.get("status") or "")
    error: str | None = None
    retry: str | None = None
    timed_out = False
    entities_linked: int | None = None
    if wait:
        row = await runtime_ctx.built.ingest_service.wait_for_episode_processing(
            str(result.get("episode_id") or ""), timeout_s=60.0
        )
        status, error, retry, timed_out = _terminal_status_from_row(row, fallback=status)
        if status == "ready":
            entities_linked = await _count_linked_entities(runtime_ctx, row, str(result.get("episode_id") or ""))
    return MemoryResponse(
        episode_id=str(result.get("episode_id") or ""),
        status=status,
        session_id=session.session_id,
        flagged=body.flagged,
        bootstrap_scope=body.bootstrap_scope,
        error=error,
        retry=retry,
        timed_out=timed_out,
        entities_linked=entities_linked,
    )


@router.get("/tool-events/dirty")
async def tool_events_dirty(request: Request, project: str | None = None) -> dict:
    """Diagnostic: current dirty files + stale anchored memories (recall/visibility for Hook Center)."""
    _require_tier("readonly")
    runtime_ctx = _require_runtime_context(request)
    adapter = getattr(runtime_ctx.built, "graph_adapter", None)
    if adapter is None or not hasattr(adapter, "list_dirty_files"):
        raise HTTPException(status_code=503, detail="tool-event diagnostics unavailable")
    dirty = await asyncio.to_thread(adapter.list_dirty_files, project=project)
    stale = await asyncio.to_thread(
        adapter.stale_anchored_memories, project=project,
        namespace=_resolve_namespace(request, None),
    )
    return {"dirty_files": dirty, "stale_anchors": stale,
            "counts": {"dirty_files": len(dirty), "stale_anchors": len(stale)}}


@router.get("/tool-events/stale")
async def tool_events_stale(request: Request, project: str | None = None,
                            limit: int = 200) -> dict:
    """Readonly: return stale anchored memories without the full dirty-file diagnostic."""
    _require_tier("readonly")
    runtime_ctx = _require_runtime_context(request)
    adapter = getattr(runtime_ctx.built, "graph_adapter", None)
    if adapter is None or not hasattr(adapter, "stale_anchored_memories"):
        raise HTTPException(status_code=503, detail="tool-event diagnostics unavailable")
    stale = await asyncio.to_thread(
        adapter.stale_anchored_memories, project=project, limit=limit,
        namespace=_resolve_namespace(request, None),
    )
    return {"stale_anchors": stale, "count": len(stale)}


@router.post("/tool-events/stale-verifications", response_model=StaleAnchorVerificationResponse)
async def record_stale_anchor_verification(request: Request,
                                           body: StaleAnchorVerificationRequest) -> StaleAnchorVerificationResponse:
    """Record a stale-anchor verification receipt (audit-only). Validates outcome and
    stores a durable StaleAnchorVerification node. Does not clear dirty flags or refresh
    structure."""
    _require_tier("agent")
    runtime_ctx = _require_runtime_context(request)
    adapter = getattr(runtime_ctx.built, "graph_adapter", None)
    if adapter is None or not hasattr(adapter, "record_stale_anchor_verification"):
        raise HTTPException(status_code=503, detail="stale-anchor verification unavailable")
    from datetime import datetime, timezone
    verified_at = body.verified_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        result = await asyncio.to_thread(
            adapter.record_stale_anchor_verification,
            memory_uuid=body.memory_uuid, namespace=_resolve_namespace(request, None),
            project=body.project, path=body.path,
            outcome=body.outcome, verified_by=body.verified_by, basis=body.basis,
            current_file_hash=body.current_file_hash, notes=body.notes,
            verified_at=verified_at,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return StaleAnchorVerificationResponse(
        accepted=bool(result.get("accepted", False)),
        verification=result.get("verification", {}),
    )


@router.get("/tool-events/stale-verifications")
async def list_stale_anchor_verifications(
    request: Request, memory_uuid: str | None = None,
    project: str | None = None, path: str | None = None,
    limit: int = 50,
) -> dict:
    """Return stored stale-anchor verification receipts, optionally filtered."""
    _require_tier("readonly")
    runtime_ctx = _require_runtime_context(request)
    adapter = getattr(runtime_ctx.built, "graph_adapter", None)
    if adapter is None or not hasattr(adapter, "list_stale_anchor_verifications"):
        raise HTTPException(status_code=503, detail="stale-anchor verification unavailable")
    rows = await asyncio.to_thread(
        adapter.list_stale_anchor_verifications,
        namespace=_resolve_namespace(request, None),
        memory_uuid=memory_uuid, project=project, path=path, limit=limit,
    )
    return {"verifications": rows, "count": len(rows)}


@router.delete("/memory/{uuid}")
async def delete_memory(request: Request, uuid: str) -> dict:
    _require_tier("operator")
    _try_record_destructive_op_rest("delete_memory")
    backend = _get_backend(request)
    # Deliberately still delete_memory, not erase_memory. Both run the same erasure saga -- the
    # only difference is that this one flattens the outcome to a bool. Switching the REST route
    # to the richer call is an external API change with its own contract test, so it is a
    # separate decision rather than a side effect of adding erase_memory.
    deleted = await backend.delete_memory(uuid)
    return {"uuid": uuid, "deleted": deleted}


@router.delete("/namespace/{namespace}")
async def delete_namespace(
    request: Request,
    namespace: str,
    max_nodes: Annotated[int, Query(ge=0)] = 200,
    force: bool = False,
    dry_run: bool = False,
) -> dict:
    """Tear down a namespace silo. Refuses the default/shared namespace (400).

    Safety gate: refuses (400) when the namespace has more than `max_nodes` nodes,
    unless `force=true`. Use `dry_run=true` to inspect the node count first without
    deleting anything -- this is a blast-radius guard, not a backup.
    """
    _require_tier("operator")
    backend = _get_backend(request)
    try:
        result = await backend.delete_namespace(
            namespace, max_nodes=max_nodes, force=force, dry_run=dry_run
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not dry_run:
        _try_record_destructive_op_rest("delete_namespace")
    return result


@router.post("/memory/{uuid}/flag", response_model=FlagResponse)
async def flag_memory(
    request: Request, uuid: str, bootstrap_scope: str | None = None
) -> FlagResponse:
    _require_tier("agent")
    backend = _get_backend(request)
    if bootstrap_scope is None:
        flagged = await backend.flag_memory(uuid)
    else:
        flagged = await backend.flag_memory(uuid, bootstrap_scope=bootstrap_scope)
    fetch_by_uuid = getattr(backend, "fetch_memory_by_uuid", None)
    row = await fetch_by_uuid(uuid) if flagged and fetch_by_uuid is not None else None
    return FlagResponse(
        uuid=uuid,
        flagged=flagged,
        bootstrap_scope=(row or {}).get("bootstrap_scope"),
    )


@router.post("/memory/{uuid}/unflag", response_model=UnflagResponse)
async def unflag_memory(request: Request, uuid: str) -> UnflagResponse:
    _require_tier("agent")
    backend = _get_backend(request)
    unflagged = await backend.unflag_memory(uuid)
    return UnflagResponse(uuid=uuid, unflagged=unflagged)


@router.get("/stats", response_model=StatsResponse)
async def stats(
    request: Request,
    since_hours: Annotated[int, Query(ge=1, le=168)] = 24,
) -> StatsResponse:
    runtime_ctx = _require_runtime_context(request)
    backend = _get_backend(request)
    built = runtime_ctx.built
    op_stats = await backend.fetch_operation_stats(since_hours=since_hours)
    enrichment = await backend.fetch_enrichment_rate(since_hours=since_hours)
    queue_depth = await backend.get_queue_depth()
    scheduler_snapshot = await backend.scheduler_status_snapshot()
    capabilities = runtime_ctx.capabilities
    return StatsResponse(
        since_hours=since_hours,
        startup_mode=capabilities.startup_mode if capabilities is not None else None,
        capabilities=_capability_payload(runtime_ctx),
        services=_service_payload(runtime_ctx),
        queue_depth=queue_depth,
        enrichment_enabled=bool(built.ingest_service and built.ingest_service.enrichment_enabled()),
        scheduler=scheduler_snapshot,
        enrichment=enrichment,
        operations=op_stats,
    )


@router.post("/phase3/reset", response_model=Phase3ResetResponse)
async def phase3_reset(
    request: Request,
    namespace: Annotated[str, Query(min_length=1)],
) -> Phase3ResetResponse:
    """Tear down a throwaway Phase 3 namespace: the graphiti group partition (Views + watermark,
    via the default-refusing delete) PLUS its `:TurnEvidence` (namespace-keyed, not covered by the
    group_id-based partition delete). Refuses the default/shared namespace."""
    return await phase3_reset_impl(
        request,
        namespace,
        require_tier=_require_tier,
        try_record_destructive_op_rest=_try_record_destructive_op_rest,
        require_phase3_adapter=_require_phase3_adapter,
        get_backend=_get_backend,
    )
