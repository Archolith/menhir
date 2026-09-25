"""Observation, scalar-history advisory, and view-authority suppression lanes for recall."""

from __future__ import annotations

import asyncio
import logging
import math
from time import perf_counter
from typing import Any

from menhir.domain.models import FreshnessState, NodeScope
from menhir.domain.namespace import stamped_namespace
from menhir.domain.recall import (
    RetrievalScoreKind,
    ScalarAuthorityContributor,
    ScalarAuthorityVerdict,
)
from menhir.domain.retrieval_tuning import CandidateSource
from menhir.services.recall_pipeline_scalar_authority import (
    _inject_scalar_authority,
    _projection_is_recall_eligible,
)
from menhir.services.recall_policies import (
    _query_wants_history,
    _render_scalar_history_content,
)

logger = logging.getLogger(__name__)


async def _run_scalar_lanes(
    service: Any,
    query: str,
    namespace: str | None,
    candidate_inputs: list[dict[str, object]],
    metadata_by_uuid: dict[str, dict[str, object]],
    authority_layer: list[ScalarAuthorityVerdict],
    _t_phases: dict[str, int],
) -> tuple[set[tuple[str, str, str, str, str]], list[dict[str, object]]]:
    """Run the observation, scalar-history advisory, and view-authority suppression lanes."""
    # Pre-initialize: populated by the observation lane if it runs; read by the scalar_history
    # advisory lane below regardless of whether authority injection is enabled.
    _obs_slots_for_history: set[tuple[str, str, str, str, str]] = set()
    # --- Observation candidates (Phase 4a.2, flag-gated): inject :TypedAssertion observations ---
    # The recall pipeline is otherwise :Entity-only (fetch_candidate_metadata matches (n:Entity)),
    # so a typed-scalar observation ("I own 20 rare coins") is never a candidate. Search the
    # observation lane whenever either scalar feature needs query-dependent slots. Only
    # scalar_view_authority_enabled adds observations to candidate_inputs and performs the current-
    # value authority work below; history-only mode uses the same hits as a neutral discovery seam.
    # Injected HERE (before the empty-candidate early-return below) so observations can surface even
    # when NO :Entity candidate matched -- the exact "the fact lives only on the assertion log" case.
    # Only MATERIALIZABLE observations surface (the lane query filters superseded/binding_pending).
    # Flag-OFF -> no search, byte-identical recall. Failures never break recall.
    if (service.scalar_view_authority_enabled or service.scalar_history_enabled) and namespace is not None:
        _t = perf_counter()
        try:
            _obs_slots_for_history = await _run_observation_lane(
                service, query, namespace, candidate_inputs, metadata_by_uuid, authority_layer
            )
        except Exception:
            logger.exception(
                "observation injection failed query=%r; leaving recall unchanged", query[:60]
            )
        _t_phases["observation_lane"] = int((perf_counter() - _t) * 1000)
    # --- Scalar history advisory lane (flag-gated) ---
    # When scalar_history_enabled, surface scalar_history Views as advisory context for
    # slots discovered by the observation lane above. This is the Slice 3 recall contract:
    # - activate for history/change/comparison questions;
    # - activate as bounded support when a current-state query has no anchored scalar state;
    # - remain below an authoritative scalar_state head when both exist;
    # - label delta-only slots "advisory history — not an absolute current total";
    # - include contributor IDs for provenance inspection.
    # Must NOT: pass the current-anchor foundation check, enter the scalar authority layer,
    # suppress raw memories, convert latest delta to absolute, add delta entries,
    # use recorded/ingest time as the displayed event time.
    # Failures never break recall. Flag-OFF -> the generic exclusion above already prevents
    # scalar_history Views from leaking through vector/entity retrieval.
    #
    if service.scalar_history_enabled and namespace is not None and _obs_slots_for_history:
        _t = perf_counter()
        try:
            await _run_scalar_history_lane(
                service, query, namespace, candidate_inputs, metadata_by_uuid,
                authority_layer, _obs_slots_for_history,
            )
        except Exception:
            logger.exception(
                "scalar-history advisory lane failed query=%r; leaving recall unchanged",
                query[:60],
            )
        _t_phases["scalar_history_lane"] = int((perf_counter() - _t) * 1000)
    # --- Step 7 canary: current-state View authority suppression (flag-gated) ---
    # When enabled, a current scalar_state View may suppress an older PROVENANCE-LINKED graph
    # fact for an explicit current-state query, but only if all six authority gates pass. Failures
    # never break recall (logged, recall proceeds unchanged). OFF by default -> no behavior change.
    if service.scalar_view_authority_enabled and namespace is not None and candidate_inputs:
        _t = perf_counter()
        try:
            suppressed = service._plan_view_authority_suppression(query, namespace, candidate_inputs)
            if suppressed:
                candidate_inputs = [
                    c for c in candidate_inputs if str(c["uuid"]) not in suppressed
                ]
        except Exception:
            logger.exception(
                "View-authority suppression failed query=%r; leaving recall unchanged", query[:60]
            )
        _t_phases["view_authority"] = int((perf_counter() - _t) * 1000)
    return _obs_slots_for_history, candidate_inputs


async def _run_observation_lane(
    service: Any,
    query: str,
    namespace: str | None,
    candidate_inputs: list[dict[str, object]],
    metadata_by_uuid: dict[str, dict[str, object]],
    authority_layer: list[ScalarAuthorityVerdict],
) -> set[tuple[str, str, str, str, str]]:
    """Search the observation lane, inject observations, and run scalar-authority injection."""
    # Phase 4b instrumentation: the additive authority path emits its own recall_audit events
    # (the legacy suppression path's events cover only suppression). One `authority_annotation`
    # event per injected slot records whether it LEADS or stays ADVISORY and the basis
    # (tier vs user-FOUNDS) -- so a MEASURE run can compute the wrongful-authority rate (a
    # `leads` with no user foundation) and confirm the lane fired. No-op when the toggle is off.
    from menhir.infrastructure.audit_trail import RECALL as _authority_audit
    if service.scalar_view_authority_enabled:
        _authority_audit.begin()
    obs_vec = await service.graphiti_client.embed_query(query)
    obs_hits = await asyncio.to_thread(
        service.graph_adapter.search_assertion_embeddings,
        obs_vec, limit=10, namespaces=[stamped_namespace(namespace)],
    )
    existing_uuids = (
        {str(c["uuid"]) for c in candidate_inputs}
        if service.scalar_view_authority_enabled else set()
    )
    obs_added = 0
    obs_slots: set[tuple[str, str, str, str, str]] = set()
    # G17 (4a.3): subject_uuid -> subject_display of each surfaced observation, used to
    # resolve the QUERY's subject INDEPENDENTLY of the injection's provenance row (a named
    # third party the query mentions is rescued by matching its display in the query text).
    obs_subject_displays: dict[str, str] = {}
    for hit in obs_hits:
        try:
            aid = str(hit.get("assertion_id") or "").strip()
            span = str(hit.get("stated_span") or "").strip()
            cos = hit.get("cosine")
            if not aid or not span or cos is None or not math.isfinite(float(cos)):
                continue
            _subj = str(hit.get("subject_uuid") or "")
            _attr = str(hit.get("attribute") or "")
            _scope = str(hit.get("scope") or "")
            _value_kind = str(hit.get("value_kind") or "")
            _unit = str(hit.get("unit") or "")
            if _subj and _attr and _value_kind:
                obs_slots.add((_subj, _attr, _scope, _value_kind, _unit))
            # History-only mode deliberately collects the matched slot but does not inject
            # the raw observation or run scalar-state authority/suppression work.
            if not service.scalar_view_authority_enabled:
                continue
            if aid in existing_uuids:
                continue
            existing_uuids.add(aid)
            candidate_inputs.append({
                "uuid": aid, "name": span, "content": span,
                "scope": str(NodeScope.PERSISTENT), "memory_type": "OBSERVATION",
                "similarity": float(cos), "last_accessed_days_ago": 0.0, "edge_count": 0,
                "freshness": str(FreshnessState.ACTIVE), "has_conflict": False,
                "conflict_status": None, "source": CandidateSource.OBSERVATION,
                "contributing_sources": frozenset({CandidateSource.OBSERVATION}),
                "retrieval_score_kind": RetrievalScoreKind.GRAPHITI_RRF,
                "bm25_rank": None, "cosine_rank": None, "content_rank": None,
                "content_cosine": None, "is_superseded_view": False, "view_kind": None,
            })
            # Seed metadata so any oracle/provenance reader (and Phase 4a.4's slot-keyed
            # View-authority lookup) can resolve the observation's slot + subject.
            metadata_by_uuid[aid] = {
                "name": span, "content": span, "namespace": hit.get("namespace"),
                "valid_at": hit.get("valid_at"),
                "ss_attribute": hit.get("attribute"), "ss_scope": hit.get("scope"),
                "ss_value_kind": hit.get("value_kind"), "ss_unit": hit.get("unit"),
                "subject_uuid": hit.get("subject_uuid"),
            }
            if _subj:
                obs_subject_displays[_subj] = str(hit.get("subject_display") or "")
            obs_added += 1
        except Exception as exc:
            logger.error(
                "Recall skipped malformed observation hit id=%r: %s: %s",
                hit.get("assertion_id"), exc.__class__.__name__, exc, exc_info=True,
            )
    logger.debug("observation injection query=%r added=%d (k=10)", query[:60], obs_added)
    _obs_slots_for_history = obs_slots  # share with scalar_history lane below
    auth_added, _intent = await _inject_scalar_authority(
        service, query, namespace, candidate_inputs, metadata_by_uuid,
        authority_layer, existing_uuids, obs_slots, obs_subject_displays, obs_added,
    )
    # Lane summary: how many observations surfaced and how many authority annotations fired,
    # under which intent -- the top-line the MEASURE run reads to confirm the lane ran.
    if service.scalar_view_authority_enabled:
        _authority_audit.audit(
            "observation_lane", "surfaced", namespace=stamped_namespace(namespace),
            details={"observations_added": obs_added,
                     "authority_annotations": auth_added,
                     "intent": _intent.value, "query": query[:120]})
    return _obs_slots_for_history


async def _run_scalar_history_lane(
    service: Any,
    query: str,
    namespace: str | None,
    candidate_inputs: list[dict[str, object]],
    metadata_by_uuid: dict[str, dict[str, object]],
    authority_layer: list[ScalarAuthorityVerdict],
    _obs_slots_for_history: set[tuple[str, str, str, str, str]],
) -> None:
    """Surface scalar_history Views as advisory context for observation-lane slots."""
    from menhir.domain.scalar_view_authority import QueryIntent
    from menhir.domain.scalar_view_suppression import authority_query_intent
    _sh_intent = authority_query_intent(query)
    _sh_wants_history = (
        _query_wants_history(query)
        or _sh_intent in (QueryIntent.COMPARISON, QueryIntent.PREVIOUS_VALUE)
    )
    _sh_existing = {str(c["uuid"]) for c in candidate_inputs}
    _sh_added = 0
    for (subj, attr, scp, vk, un) in _obs_slots_for_history:
        if not subj or not attr or not vk:
            continue
        # Read gate: history/comparison → always; current-state → only when
        # no scalar_state View exists (bounded support for unanchored slots).
        if not _sh_wants_history:
            _state_view = await asyncio.to_thread(
                service.graph_adapter.fetch_current_scalar_view_for_slot,
                subject_uuid=subj, attribute=attr, scope=scp,
                value_kind=vk, unit=un,
                namespace=stamped_namespace(namespace),
            )
            if _state_view and _projection_is_recall_eligible(_state_view):
                continue  # scalar_state leads; history stays off
        hv = await asyncio.to_thread(
            service.graph_adapter.fetch_scalar_history,
            subject_uuid=subj, attribute=attr, scope=scp,
            value_kind=vk, unit=un,
            namespace=stamped_namespace(namespace),
        )
        if not hv or not _projection_is_recall_eligible(hv):
            continue
        huuid = str(hv.get("uuid") or "").strip()
        if not huuid or huuid in _sh_existing:
            continue
        _sh_existing.add(huuid)
        hcontent = _render_scalar_history_content(hv)
        hname = (
            f"advisory history: {attr} ({scp})"
            if scp else f"advisory history: {attr}"
        )
        candidate_inputs.append({
            "uuid": huuid, "name": hname, "content": hcontent,
            "scope": str(NodeScope.PERSISTENT),
            "memory_type": "SCALAR_HISTORY",
            # Below authority (1.0) but above generic floor — advisory rank.
            "similarity": 0.85,
            "last_accessed_days_ago": 0.0, "edge_count": 0,
            "freshness": str(FreshnessState.ACTIVE),
            "has_conflict": False, "conflict_status": None,
            "source": CandidateSource.OBSERVATION,
            "contributing_sources": frozenset({CandidateSource.OBSERVATION}),
            "retrieval_score_kind": RetrievalScoreKind.SOURCE_PRIOR,
            "bm25_rank": None, "cosine_rank": None, "content_rank": None,
            "content_cosine": None, "is_superseded_view": False,
            "view_kind": "scalar_history",
            "is_scalar_authority": False,  # NEVER authority
        })
        metadata_by_uuid[huuid] = {
            "name": hname, "content": hcontent,
            "namespace": stamped_namespace(namespace),
            "view_kind": "scalar_history", "view_current": True,
            "entry_count": int(hv.get("entry_count") or 0),
            "payload_entry_count": int(
                hv.get("payload_entry_count")
                if hv.get("payload_entry_count") is not None
                else len(hv.get("entries") or [])
            ),
            "omitted_entry_count": int(hv.get("omitted_entry_count") or 0),
        }
        authority_layer.append(ScalarAuthorityVerdict(
            kind="history", status="advisory",
            subject_uuid=subj, attribute=attr, scope=scp,
            value_kind=vk, unit=un,
            value=None, valid_at=hv.get("last_valid_at"),
            view_uuid=huuid, has_foundation=False,
            contributors=tuple(
                ScalarAuthorityContributor(
                    assertion_id=str(e.get("assertion_id") or ""),
                    relation="HISTORY_ENTRY",
                    operation=str(e.get("operation") or ""),
                    value=e.get("value"),
                    stated_span=str(e.get("stated_span") or ""),
                    valid_at=str(e.get("valid_at") or ""),
                    evidence_tier=str(e.get("evidence_tier") or ""),
                    episode_uuid=str(e.get("episode_uuid") or ""),
                )
                for e in (hv.get("entries") or [])
            ),
            contributors_total=int(hv.get("entry_count") or 0),
            contributors_truncated=bool(int(hv.get("omitted_entry_count") or 0) > 0),
            next_offset=(
                int(hv.get("payload_entry_count") or len(hv.get("entries") or []))
                if int(hv.get("omitted_entry_count") or 0) > 0 else None
            ),
        ))
        _sh_added += 1
    logger.debug(
        "scalar-history advisory lane query=%r added=%d", query[:60], _sh_added)
