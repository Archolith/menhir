"""REST API endpoints for remote memory access."""

from __future__ import annotations

import asyncio
import logging
from uuid import uuid4
from typing import Any
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request

from menhir.core.backend_impl import _drain_background_errors
from menhir.domain.bootstrap_scope import bootstrap_selection
from menhir.domain.recall import InvalidQueryPresetError
from menhir.domain.session import new_session
from menhir.domain.structural_memory import is_structural_memory_row
from menhir.mcp.service_access import get_request_session

from .routes_handlers import (
    backend_invoke_impl,
    list_clients_impl,
    mint_client_impl,
    phase3_reset_impl,
    phase3_run_impl,
    phase3_status_impl,
    phase3_views_impl,
    revoke_client_impl,
)

from .routes_support import (
    BootstrapContextRequest,
    ClientSummary,
    ContextRequest,
    ContextResponse,
    FlagResponse,
    HealthResponse,
    ListClientsResponse,
    MemoryRequest,
    MemoryResponse,
    EpisodeAdmissionRequest,
    EpisodeAdmissionResponse,
    MintClientRequest,
    MintClientResponse,
    Phase3ResetResponse,
    Phase3RunRequest,
    Phase3RunResponse,
    Phase3StatusResponse,
    Phase3ViewsResponse,
    ReadyResponse,
    RecallMemory,
    RecallRequest,
    RecallResponse,
    RevokeClientResponse,
    RuntimeContext,
    StaleAnchorVerificationRequest,
    StaleAnchorVerificationResponse,
    StatsResponse,
    ToolEventRequest,
    ToolEventResponse,
    TurnEvidenceRequest,
    TurnEvidenceResponse,
    UnflagResponse,
    _BACKEND_METHODS,
    _OP_TIER_AGENT,
    _OP_TIER_OPERATOR,
    _VALID_CLIENT_TIERS,
    _capability_payload,
    _get_backend,
    _get_client_token_store,
    _get_runtime_context,
    _require_phase3_adapter,
    _require_runtime_context,
    _require_tier,
    _required_tier_for_operation,
    _resolve_caller_session,
    _resolve_namespace,
    _service_payload,
    _try_record_destructive_op_rest,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")
_INTERNAL_BACKEND_PREFIX = "/internal/backend"


@router.get("/health", response_model=HealthResponse)
async def health(request: Request) -> HealthResponse:
    runtime_ctx = _get_runtime_context(request)
    capabilities = runtime_ctx.capabilities if runtime_ctx is not None else None
    settings = getattr(request.app.state, "settings", None)
    status = "ok" if runtime_ctx is not None else "starting"
    return HealthResponse(
        status=status,
        services=_service_payload(runtime_ctx),
        startup_mode=capabilities.startup_mode if capabilities is not None else None,
        instance_id=str(settings.instance_id) or None if settings is not None else None,
    )


@router.get("/ready", response_model=ReadyResponse)
async def ready(request: Request) -> ReadyResponse:
    runtime_ctx = _get_runtime_context(request)
    capabilities = runtime_ctx.capabilities if runtime_ctx is not None else None
    if capabilities is None:
        return ReadyResponse(
            status="starting",
            startup_mode="unavailable",
            capabilities=_capability_payload(None),
            failures=["runtime not initialized"],
        )
    status = "ready" if capabilities.enrichment_ready else "degraded"
    return ReadyResponse(
        status=status,
        startup_mode=capabilities.startup_mode,
        capabilities=_capability_payload(runtime_ctx),
        failures=list(capabilities.failures),
    )


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


@router.get("/scalar-authority/{view_uuid}/contributors")
async def scalar_authority_contributors(
    request: Request,
    view_uuid: str,
    namespace: str | None = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 8,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, object]:
    """Expand a structured scalar-authority verdict's bounded provenance window."""
    _require_tier("readonly")
    runtime_ctx = _require_runtime_context(request)
    adapter = getattr(runtime_ctx.built, "graph_adapter", None)
    if adapter is None or not hasattr(adapter, "fetch_scalar_authority_contributors"):
        raise HTTPException(status_code=503, detail="scalar authority provenance unavailable")
    resolved = _resolve_namespace(request, namespace)
    from menhir.domain.namespace import stamped_namespace
    payload = await asyncio.to_thread(
        adapter.fetch_scalar_authority_contributors,
        view_uuid=view_uuid, limit=limit, offset=offset,
        namespace=stamped_namespace(resolved),
    )
    return {"view_uuid": view_uuid, **payload}


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
    rows = await backend.fetch_flagged_memories(limit=limit, workspace=workspace)
    rows = [row for row in rows if not is_structural_memory_row(row)]
    version = await backend.fetch_flagged_memory_bootstrap_version(workspace=workspace)
    _remember_flagged_bootstrap_read(normalized_reader, version, workspace=workspace)
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
    effective_workspace = body.workspace or body.namespace
    selection_key, _allowed = bootstrap_selection(effective_workspace)
    version = await backend.fetch_flagged_memory_bootstrap_version(
        workspace=effective_workspace
    )
    if not _has_recent_flagged_bootstrap_read(
        normalized_reader, version, workspace=effective_workspace
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
            namespace=body.namespace,
        )
        relevant = [
            _compact_scored_item(SimpleNamespace(**row))
            for row in recalled.get("results", [])
        ]

    recent_rows = await backend.fetch_recent_memories(
        limit=body.recent_limit * 3,
        namespace=body.namespace,
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
    if wait:
        await runtime_ctx.built.ingest_service.wait_for_episode_processing(str(result.get("episode_id") or ""), timeout_s=60.0)
    return MemoryResponse(
        episode_id=str(result.get("episode_id") or ""),
        status=str(result.get("status") or ""),
        session_id=session.session_id,
        flagged=body.flagged,
        bootstrap_scope=body.bootstrap_scope,
    )


@router.post("/turn-evidence", response_model=TurnEvidenceResponse)
async def record_turn_evidence(request: Request, body: TurnEvidenceRequest) -> TurnEvidenceResponse:
    """Capture one candidate user turn as a `:TurnEvidence` node (ADR 0001). Idempotent on turn_key.
    Selective evidence only — the producer triages before posting; the server runs no LLM. Raw
    evidence never enters normal recall; Phase 3 reads role=user evidence."""
    _require_tier("agent")
    runtime_ctx = _require_runtime_context(request)
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")
    if body.role not in ("user", "assistant", "tool", "agent"):
        raise HTTPException(status_code=400, detail="role must be one of user|assistant|tool|agent")
    adapter = getattr(runtime_ctx.built, "graph_adapter", None)
    if adapter is None or not hasattr(adapter, "record_turn_evidence"):
        raise HTTPException(status_code=503, detail="turn-evidence capture unavailable")
    try:
        result = await asyncio.to_thread(
            adapter.record_turn_evidence,
            text=text,
            role=body.role,
            declarant=body.declarant,
            session_id=body.session_id,
            occurred_at=body.occurred_at,
            namespace=_resolve_namespace(request, body.namespace),
            source_kind=body.source_kind,
            source_id=body.source_id,
            source_client=body.source_client,
            hook_version=body.hook_version,
            cwd=body.cwd,
            transcript_path=body.transcript_path,
            triage_reason=body.triage_reason,
            triage_version=body.triage_version,
            metadata=body.metadata,
            turn_key=body.turn_key,
            prompt_id=body.prompt_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return TurnEvidenceResponse(
        turn_id=result["turn_id"],
        created=result["created"],
        recorded_at=result["recorded_at"],
        occurred_at=result.get("occurred_at"),
    )


@router.post("/episode-admission", response_model=EpisodeAdmissionResponse)
async def link_episode_admission(
    request: Request, body: EpisodeAdmissionRequest
) -> EpisodeAdmissionResponse:
    """Join a memory to the captured turn it answered, and project that turn's text.

    A host's post-tool lifecycle event fires AFTER `add_memory` has run, so it cannot pass
    `turn_evidence_uuid` on the original call; it reports the pairing here instead. Draws
    `(:Episodic)-[:ADMITTED_ON]->(:TurnEvidence)` and mints the evidence projection, so a scalar
    assertion extracted from the user's words can reach entities extracted from those same words.

    MATCH-only on both endpoints: an id naming nothing draws nothing and MERGEs no stub, so a wrong
    or stale id is inert rather than corrupting.

    NOT A VERIFICATION STEP. Both ids come from the caller and the server cannot confirm the memory
    actually came from that turn. It grants nothing a same-key client lacks -- such a client can
    already write a `:TurnEvidence` claiming `declarant='user'` -- but the resulting edge must not be
    read as proof a human spoke. Same `agent` tier as turn capture, for exactly that reason.
    """
    _require_tier("agent")
    runtime_ctx = _require_runtime_context(request)
    episode_uuid = (body.episode_uuid or "").strip()
    turn_evidence_uuid = (body.turn_evidence_uuid or "").strip()
    if not episode_uuid or not turn_evidence_uuid:
        raise HTTPException(
            status_code=400, detail="episode_uuid and turn_evidence_uuid are both required")
    adapter = getattr(runtime_ctx.built, "graph_adapter", None)
    if adapter is None or not hasattr(adapter, "link_episode_admission"):
        raise HTTPException(status_code=503, detail="episode admission unavailable")

    linked = bool(await asyncio.to_thread(
        adapter.link_episode_admission,
        episode_uuid=episode_uuid, turn_evidence_uuid=turn_evidence_uuid,
    ))

    # Only project once the join actually landed. Projecting after a failed link would enrich a turn
    # that no memory references, which is capture-volume enrichment -- the cost ADR 0001 rejected.
    projection_uuid = None
    if linked and hasattr(adapter, "create_evidence_projection"):
        session = get_request_session()
        projection_uuid = await asyncio.to_thread(
            adapter.create_evidence_projection,
            turn_evidence_uuid=turn_evidence_uuid,
            projection_uuid=str(uuid4()),
            name=f"evidence-projection-{turn_evidence_uuid}",
            session_id=getattr(session, "session_id", "") or "",
            user_id=getattr(session, "user_id", "") or "",
            namespace=_resolve_namespace(request, None),
        )
    return EpisodeAdmissionResponse(linked=linked, projection_uuid=projection_uuid)


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


@router.get("/tool-events/dirty")
async def tool_events_dirty(request: Request, project: str | None = None) -> dict:
    """Diagnostic: current dirty files + stale anchored memories (recall/visibility for Hook Center)."""
    _require_tier("readonly")
    runtime_ctx = _require_runtime_context(request)
    adapter = getattr(runtime_ctx.built, "graph_adapter", None)
    if adapter is None or not hasattr(adapter, "list_dirty_files"):
        raise HTTPException(status_code=503, detail="tool-event diagnostics unavailable")
    dirty = await asyncio.to_thread(adapter.list_dirty_files, project=project)
    stale = await asyncio.to_thread(adapter.stale_anchored_memories, project=project)
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
    stale = await asyncio.to_thread(adapter.stale_anchored_memories, project=project, limit=limit)
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
            memory_uuid=body.memory_uuid, project=body.project, path=body.path,
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
        memory_uuid=memory_uuid, project=project, path=path, limit=limit,
    )
    return {"verifications": rows, "count": len(rows)}


@router.delete("/memory/{uuid}")
async def delete_memory(request: Request, uuid: str) -> dict:
    _require_tier("operator")
    _try_record_destructive_op_rest("delete_memory")
    backend = _get_backend(request)
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

@router.post("/phase3/run", response_model=Phase3RunResponse)
async def phase3_run(request: Request, body: Phase3RunRequest) -> Phase3RunResponse:
    """Run one personal-memory View consolidation pass over a single namespace (black-box eval
    surface for archolith-bench). Mirrors the scheduler job: real LLM, all bias guards pinned on,
    batch re-fold, isolated to the explicit namespace so it never touches other silos."""
    return await phase3_run_impl(
        request,
        body,
        require_tier=_require_tier,
        require_phase3_adapter=_require_phase3_adapter,
    )


@router.get("/phase3/status", response_model=Phase3StatusResponse)
async def phase3_status(
    request: Request,
    namespace: Annotated[str, Query(min_length=1)],
) -> Phase3StatusResponse:
    """Report whether a namespace is dirty (Phase 3 would consolidate it) and its evidence count."""
    return await phase3_status_impl(
        request,
        namespace,
        require_phase3_adapter=_require_phase3_adapter,
    )


@router.get("/views", response_model=Phase3ViewsResponse)
async def phase3_views(
    request: Request,
    namespace: Annotated[str, Query(min_length=1)],
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> Phase3ViewsResponse:
    """List current counter Views for a namespace, each with full value history (current +
    superseded, so currentness/supersession is inspectable). USER Views and `subject='perception'`
    abstention receipts (F1 observability, out of recall) are returned separately."""
    return await phase3_views_impl(
        request,
        namespace,
        limit,
        require_phase3_adapter=_require_phase3_adapter,
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


@router.post(f"{_INTERNAL_BACKEND_PREFIX}/{{operation}}", include_in_schema=False)
async def backend_invoke(request: Request, operation: str, body: dict[str, Any] | None = None) -> Any:
    return await backend_invoke_impl(
        request,
        operation,
        body,
        backend_methods=_BACKEND_METHODS,
        required_tier_for_operation=_required_tier_for_operation,
        require_tier=_require_tier,
        try_record_destructive_op_rest=_try_record_destructive_op_rest,
        resolve_caller_session=_resolve_caller_session,
        get_backend=_get_backend,
        drain_background_errors=_drain_background_errors,
        logger=logger,
    )


# ---------------------------------------------------------------------------
# Per-client token tier — admin endpoints (mint / revoke)
# ---------------------------------------------------------------------------
# Auth for /api/admin/* is enforced by BearerAuthMiddleware's admin gate
# (operator key or loopback origin), which binds the operator tier before the
# handler runs. _require_tier("operator") below is defense-in-depth.

@router.post("/admin/clients", response_model=MintClientResponse)
async def mint_client(request: Request, body: MintClientRequest) -> MintClientResponse:
    return await mint_client_impl(
        request,
        body,
        require_tier=_require_tier,
        valid_client_tiers=_VALID_CLIENT_TIERS,
        get_client_token_store=_get_client_token_store,
        try_record_destructive_op_rest=_try_record_destructive_op_rest,
        get_request_session_func=get_request_session,
    )


@router.get("/admin/clients", response_model=ListClientsResponse)
async def list_clients(request: Request) -> ListClientsResponse:
    return await list_clients_impl(
        request,
        require_tier=_require_tier,
        get_client_token_store=_get_client_token_store,
    )


@router.post("/admin/clients/{client_id}/revoke", response_model=RevokeClientResponse)
async def revoke_client(request: Request, client_id: str) -> RevokeClientResponse:
    return await revoke_client_impl(
        request,
        client_id,
        require_tier=_require_tier,
        get_client_token_store=_get_client_token_store,
        try_record_destructive_op_rest=_try_record_destructive_op_rest,
    )
