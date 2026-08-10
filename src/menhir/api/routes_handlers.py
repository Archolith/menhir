"""Extracted handler implementations for REST API routes."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from menhir.domain.recall import InvalidQueryPresetError

from .routes_support import (
    ClientSummary,
    ListClientsResponse,
    MintClientRequest,
    MintClientResponse,
    Phase3ResetResponse,
    Phase3RunRequest,
    Phase3RunResponse,
    Phase3StatusResponse,
    Phase3ViewsResponse,
    RevokeClientResponse,
)


async def phase3_run_impl(
    request: Request,
    body: Phase3RunRequest,
    *,
    require_tier: Callable[[str], None],
    require_phase3_adapter: Callable[[Request], tuple[Any, Any]],
) -> Phase3RunResponse:
    require_tier("agent")
    runtime_ctx, adapter = require_phase3_adapter(request)
    settings = getattr(runtime_ctx.built, "settings", None)
    if settings is None:
        raise HTTPException(status_code=503, detail="phase 3 consolidation unavailable")
    from menhir.infrastructure.sync_llm import make_sync_chat
    from menhir.infrastructure.view_embedder import make_view_embedder, view_embedder_version
    from menhir.services.scheduler_tasks import consolidate_personal_memory

    pm_model = getattr(settings, "personal_memory_consolidation_chat_model", "") or None
    chat = make_sync_chat(
        settings, model=pm_model,
        max_tokens=getattr(settings, "personal_memory_consolidation_max_tokens", 2048))
    if chat is None:
        raise HTTPException(
            status_code=503,
            detail="no sync chat provider configured for personal-memory consolidation",
        )
    embed = make_view_embedder(settings)
    ns = body.namespace
    selected = ns in await asyncio.to_thread(adapter.list_dirty_namespaces, limit=500)
    result = await consolidate_personal_memory(
        adapter,
        llm_complete=chat,
        embed=embed,
        namespaces=[ns],
        k=body.k,
        source=body.source,
        call_budget=body.call_budget,
        verify_retries=getattr(settings, "personal_memory_consolidation_verify_retries", 0),
        sum_grounding=getattr(settings, "personal_memory_consolidation_sum_grounding", False),
        enable_counter_state=body.counter_state,
        enable_scalar_state=getattr(settings, "personal_memory_scalar_state_enabled", False),
        scalar_state_perceiver_version=getattr(
            settings,
            "personal_memory_scalar_state_perceiver_version",
            "v1",
        ),
        scalar_reconcile_attribute=getattr(
            settings, "personal_memory_scalar_reconcile_attribute", False),
        scalar_reconcile_scope=getattr(
            settings, "personal_memory_scalar_reconcile_scope", False),
        scalar_reconcile_subject=getattr(
            settings, "personal_memory_scalar_reconcile_subject", False),
        scalar_canonical_self=getattr(
            settings, "personal_memory_scalar_canonical_self", False),
        scalar_threshold=getattr(settings, "personal_memory_scalar_threshold", 1.0),
        scalar_embed_version=view_embedder_version(settings),
        scalar_history_enabled=getattr(
            settings, "personal_memory_scalar_history_enabled", False),
        scalar_deterministic_shadow_enabled=getattr(
            settings, "personal_memory_scalar_deterministic_shadow", False),
        scalar_deterministic_router_enabled=getattr(
            settings, "personal_memory_scalar_deterministic_router", False),
        scalar_deterministic_router_promoted_classes=getattr(
            settings, "personal_memory_scalar_deterministic_classes", ()),
        enable_event_history=getattr(
            settings, "personal_memory_event_history_enabled", False),
        event_history_perceiver_version=getattr(
            settings, "personal_memory_event_history_perceiver_version", "v1"),
        event_batch_size=getattr(
            settings, "personal_memory_event_history_batch_size", 500),
    )
    dirty_after = ns in await asyncio.to_thread(adapter.list_dirty_namespaces, limit=500)
    return Phase3RunResponse(
        namespace=ns,
        phase3_selected=selected,
        dirty_after=dirty_after,
        namespaces_dirty=int(result.get("namespaces_dirty", 0) or 0),
        namespaces_processed=int(result.get("namespaces_processed", 0) or 0),
        views_written=int(result.get("views_written", 0) or 0),
        abstained=int(result.get("abstained", 0) or 0),
        corrections_applied=int(result.get("corrections_applied", 0) or 0),
        llm_calls=int(result.get("llm_calls", 0) or 0),
        scalar_enabled=bool(getattr(settings, "personal_memory_scalar_state_enabled", False)),
        scalar_namespaces_processed=int(result.get("scalar_namespaces_processed", 0) or 0),
        scalar_states_written=int(result.get("scalar_states_written", 0) or 0),
        scalar_advisory=int(result.get("scalar_advisory", 0) or 0),
        scalar_repair_pending=int(result.get("scalar_repair_pending", 0) or 0),
        event_history_enabled=bool(
            getattr(settings, "personal_memory_event_history_enabled", False)),
        event_namespaces_processed=int(
            result.get("event_namespaces_processed", 0) or 0),
        event_namespaces_failed=int(
            result.get("event_namespaces_failed", 0) or 0),
        event_assertions_recorded=int(
            result.get("event_assertions_recorded", 0) or 0),
        event_assertions_created=int(
            result.get("event_assertions_created", 0) or 0),
        event_views_rebuilt=int(result.get("event_views_rebuilt", 0) or 0),
        event_llm_calls=int(result.get("event_llm_calls", 0) or 0),
    )


async def phase3_status_impl(
    request: Request,
    namespace: str,
    *,
    require_phase3_adapter: Callable[[Request], tuple[Any, Any]],
) -> Phase3StatusResponse:
    _, adapter = require_phase3_adapter(request)
    dirty = namespace in await asyncio.to_thread(adapter.list_dirty_namespaces, limit=500)
    turn_evidence = await asyncio.to_thread(adapter.count_turn_evidence, namespace)
    return Phase3StatusResponse(namespace=namespace, dirty=dirty, turn_evidence=int(turn_evidence))


async def phase3_views_impl(
    request: Request,
    namespace: str,
    limit: int,
    *,
    require_phase3_adapter: Callable[[Request], tuple[Any, Any]],
) -> Phase3ViewsResponse:
    _, adapter = require_phase3_adapter(request)
    counters = await asyncio.to_thread(adapter.list_counters, namespace=namespace, limit=limit)
    views: list[dict[str, Any]] = []
    receipts: list[dict[str, Any]] = []
    for row in counters:
        record = dict(row)
        subject = str(record.get("subject") or "")
        if subject == "perception":
            receipts.append(record)
            continue
        history = await asyncio.to_thread(
            adapter.counter_history,
            subject=subject,
            counter=str(record.get("counter") or ""),
            namespace=namespace,
        )
        record["current"] = True
        record["history"] = history
        record["superseded"] = [h for h in history if not h.get("current", True)]
        views.append(record)
    return Phase3ViewsResponse(
        namespace=namespace, count=len(views), views=views, receipts=receipts
    )


async def phase3_reset_impl(
    request: Request,
    namespace: str,
    *,
    require_tier: Callable[[str], None],
    try_record_destructive_op_rest: Callable[[str], None],
    require_phase3_adapter: Callable[[Request], tuple[Any, Any]],
    get_backend: Callable[[Request], Any],
) -> Phase3ResetResponse:
    require_tier("agent")
    try_record_destructive_op_rest("phase3_reset")
    _, adapter = require_phase3_adapter(request)
    backend = get_backend(request)
    try:
        deleted = await backend.delete_namespace(namespace)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    turn_evidence_deleted = await asyncio.to_thread(adapter.purge_turn_evidence, namespace)
    return Phase3ResetResponse(
        namespace=namespace,
        nodes_deleted=int(deleted.get("deleted", 0) or 0),
        turn_evidence_deleted=int(turn_evidence_deleted),
    )


async def backend_invoke_impl(
    request: Request,
    operation: str,
    body: dict[str, Any] | None,
    *,
    backend_methods: set[str],
    required_tier_for_operation: Callable[[str], str],
    require_tier: Callable[[str], None],
    try_record_destructive_op_rest: Callable[[str], None],
    resolve_caller_session: Callable[[Request], Any],
    get_backend: Callable[[Request], Any],
    drain_background_errors: Callable[[str], list[str]],
    logger: logging.Logger,
) -> Any:
    if operation not in backend_methods:
        raise RuntimeError(f"Unknown backend operation: {operation}")
    required_tier = required_tier_for_operation(operation)
    require_tier(required_tier)
    if required_tier == "operator":
        try_record_destructive_op_rest(operation)
    caller_session = resolve_caller_session(request)
    backend = get_backend(request)
    method = getattr(backend, operation)
    try:
        result = await method(**(body or {}))
    except InvalidQueryPresetError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception:
        logger.exception("backend_invoke failed: operation=%s body=%r", operation, body)
        raise
    warnings = drain_background_errors(caller_session.session_id)
    if not warnings:
        return result
    response = JSONResponse(content=result)
    encoded_warnings = __import__("json").dumps(warnings)
    response.headers["x-menhir-bg-warnings"] = encoded_warnings
    response.headers["x-yawn-bg-warnings"] = encoded_warnings
    return response


async def mint_client_impl(
    request: Request,
    body: MintClientRequest,
    *,
    require_tier: Callable[[str], None],
    valid_client_tiers: frozenset[str],
    get_client_token_store: Callable[[Request], Any],
    try_record_destructive_op_rest: Callable[[str], None],
    get_request_session_func: Callable[[], Any],
) -> MintClientResponse:
    require_tier("operator")
    if body.tier not in valid_client_tiers:
        raise HTTPException(status_code=422, detail=f"tier must be one of {sorted(valid_client_tiers)}")
    if not body.client_name.strip():
        raise HTTPException(status_code=422, detail="client_name is required")
    store = get_client_token_store(request)
    try_record_destructive_op_rest("admin.mint_client")
    from menhir.api.auth import LOOPBACK_BOOTSTRAP_ID

    session = get_request_session_func()
    is_bootstrap = session is not None and session.client_id == LOOPBACK_BOOTSTRAP_ID
    if is_bootstrap:
        minted = store.mint_bootstrap(body.client_name.strip(), body.tier)
        if minted is None:
            raise HTTPException(
                status_code=409,
                detail="A client token already exists; bootstrap mint is closed",
            )
        raw, record = minted
    else:
        raw, record = store.mint(body.client_name.strip(), body.tier)
    return MintClientResponse(
        client_id=record.client_id,
        client_name=record.client_name,
        tier=record.tier,
        token=raw,
    )


async def list_clients_impl(
    request: Request,
    *,
    require_tier: Callable[[str], None],
    get_client_token_store: Callable[[Request], Any],
) -> ListClientsResponse:
    require_tier("operator")
    store = get_client_token_store(request)
    return ListClientsResponse(
        clients=[
            ClientSummary(
                client_id=r.client_id,
                client_name=r.client_name,
                tier=r.tier,
                created_at=r.created_at,
            )
            for r in store.all()
        ]
    )


async def revoke_client_impl(
    request: Request,
    client_id: str,
    *,
    require_tier: Callable[[str], None],
    get_client_token_store: Callable[[Request], Any],
    try_record_destructive_op_rest: Callable[[str], None],
) -> RevokeClientResponse:
    require_tier("operator")
    store = get_client_token_store(request)
    try_record_destructive_op_rest("admin.revoke_client")
    revoked = store.revoke(client_id)
    if not revoked:
        raise HTTPException(status_code=404, detail="No such client_id")
    return RevokeClientResponse(client_id=client_id, revoked=True)
