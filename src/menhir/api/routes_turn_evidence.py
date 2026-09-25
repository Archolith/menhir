"""Turn-evidence capture and episode-admission pairing endpoints (ADR 0001)."""

from __future__ import annotations

import asyncio
from uuid import uuid4

from fastapi import HTTPException, Request

from menhir.mcp.service_access import get_request_session

from .routes_router import router
from .routes_support import (
    EpisodeAdmissionRequest,
    EpisodeAdmissionResponse,
    TurnEvidenceRequest,
    TurnEvidenceResponse,
    _require_runtime_context,
    _require_tier,
    _resolve_namespace,
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
    ingest_service = getattr(runtime_ctx.built, "ingest_service", None)
    if ingest_service is None or not hasattr(
        ingest_service, "enqueue_pending_episode"
    ):
        raise HTTPException(status_code=503, detail="episode enrichment queue unavailable")
    enrichment_enabled = getattr(ingest_service, "enrichment_enabled", None)
    if not callable(enrichment_enabled) or not enrichment_enabled():
        raise HTTPException(status_code=503, detail="episode enrichment is disabled")

    # Resolved once and used for BOTH the link and the projection below. They must be the same
    # value: linking in one namespace while projecting from another is precisely the split that
    # let a caller reach a foreign turn's text.
    resolved_namespace = _resolve_namespace(request, None)

    linked = bool(await asyncio.to_thread(
        adapter.link_episode_admission,
        episode_uuid=episode_uuid, turn_evidence_uuid=turn_evidence_uuid,
        namespace=resolved_namespace,
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
            namespace=resolved_namespace,
        )
        queue_uuid = projection_uuid
        if queue_uuid is None and hasattr(
            adapter, "find_pending_evidence_projection_uuid"
        ):
            # Creation is idempotent. If an earlier request created the durable PENDING node but
            # failed while enqueueing it, the retry must recover and enqueue that same node rather
            # than interpreting "already exists" as completed work.
            queue_uuid = await asyncio.to_thread(
                adapter.find_pending_evidence_projection_uuid,
                turn_evidence_uuid=turn_evidence_uuid,
                namespace=resolved_namespace,
            )
        if queue_uuid:
            # False is an idempotent outcome (already queued, or it raced into ENRICHING), not a
            # refusal. Exceptions remain visible, and a retry can recover the durable PENDING node.
            enqueued = await ingest_service.enqueue_pending_episode(queue_uuid)
            if not enqueued and not enrichment_enabled():
                # Close the small race between the pre-write capability check and enqueue. The
                # durable PENDING projection remains discoverable for a later retry.
                raise HTTPException(
                    status_code=503,
                    detail="evidence projection created but enrichment became disabled",
                )
    return EpisodeAdmissionResponse(linked=linked, projection_uuid=projection_uuid)
