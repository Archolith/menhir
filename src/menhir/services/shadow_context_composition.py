"""Stage 1 — shadow-mode context composition (observe-only, never applied).

.agent/plans/menhir-context-composition-production-integration.md lays out the 4-stage
rollout path from the Extraction Lab Phase 1-5 investigation
(.agent/plans/menhir-extraction-context-ablation-handoff.md) to production trust. This
module is Stage 1: for every ingested episode, best-effort retrieve real candidate
fact-edges, run a grounded shadow classification, apply a deterministic eligibility
filter (with an LLM tie-break only for genuine ties), and log the result -- without
ever changing what gets extracted or written to the graph.

Design contract (mirrors the existing shadow precedent in domain/retrieval_trace_models.py's
AssertionShadowTrace / FacetShadowTrace -- "shadow = recorded, never applied" -- with one
deliberate deviation: this IS the observability stage, so failures must stay visible, not
become a swallowed None):

  - When the feature flag is off, this module is never called (zero overhead).
  - When on, compose_shadow_prediction() ALWAYS returns a ShadowCompositionPrediction,
    never raises, never returns None. A total failure still produces a status-tagged
    result with `error` populated.
  - shadow_facet / shadow_state_family / shadow_scope are explicitly NOT MemoryFacetSet
    fields. They are labels synthesized live per episode by classify_shadow_context(),
    kept only in the shadow trace, never persisted to the graph. Production's real
    facet system (domain/facets.py) has no domain-topic vocabulary (no "Housing", no
    "Therapy schedule") -- this stage tests whether the missing semantic inputs can be
    synthesized reliably from real fact-edge text, it does not assume they already can.

Execution shape (why this file has two entry points instead of one):
  - snapshot_candidate_facts() is cheap and read-only. It must run BEFORE the real
    extraction call so the current episode's own about-to-be-created facts cannot leak
    into its own candidate pool. It runs INSIDE the per-namespace ingest gate.
  - run_shadow_composition_with_timeout() is the expensive part (grounded LLM call,
    optional tie-break LLM call). It must run AFTER the ingest gate is released, so
    shadow-mode work never reduces real ingest throughput or inflates same-namespace
    queue latency. It carries its own independent timeout, entirely separate from real
    episode completion.
"""

from __future__ import annotations

import asyncio
import logging
import time

from menhir.domain.facet_derivation import derive_facets
from menhir.domain.namespace import namespace_to_group_id, namespace_to_group_ids

# Facade re-exports: moved units stay importable from this module path.
from menhir.services.shadow_context_composition_models import (
    _STATUS_ABSTAINED_NO_CANDIDATES,
    _STATUS_ABSTAINED_NO_ELIGIBLE,
    _STATUS_ABSTAINED_TIE,
    _STATUS_CANDIDATE_QUERY_FAILED,
    _STATUS_MALFORMED_LLM_RESPONSE,
    _STATUS_METADATA_GENERATION_FAILED,
    _STATUS_SELECTED,
    _STATUS_TIMED_OUT,
    ExtractionCompositionShadowTrace,
    ShadowCandidateFact,
    ShadowCandidateLabels,
    ShadowCompositionPrediction,
    ShadowRankedHypothesis,
    ShadowRejection,
    _empty_prediction,
)
from menhir.services.shadow_context_composition_selection import (
    _candidate_payload,
    _parse_shadow_grounded_response,
    _select_eligible_candidate,
)
from menhir.services.shadow_context_composition_trace import build_shadow_trace, shadow_trace_to_details

logger = logging.getLogger(__name__)

# Candidate pool cap: bounds LLM prompt size and Neo4j load. Over-include up to this many
# distinct entities (literal-name match ∪ semantic search), then let fetch_candidate_fact_edges
# expand each into however many real fact-edges it actually has -- the "intentionally
# over-include, let the downstream step filter" philosophy already documented in
# extraction_lab.py's _lookup_known_entities.
_MAX_CANDIDATE_ENTITIES = 10
_MIN_LITERAL_NAME_LEN = 3
# _MAX_CANDIDATE_ENTITIES bounds how many distinct entities get queried, but a single
# entity can carry many historical fact-edges (a "Rachel" node with 20 past residence
# claims) -- without a second cap the grounded-classification prompt's size (and LLM
# token cost) is unbounded by real graph density, not by this module's own config.
_MAX_CANDIDATE_FACTS = 30


# ---------------------------------------------------------------------------
# Phase 1 (inside the ingest gate, before the real extraction call): cheap,
# read-only candidate snapshot.
# ---------------------------------------------------------------------------

async def snapshot_candidate_facts(
    graphiti_client: object,
    graph_adapter: object,
    *,
    namespace: str,
    episode_body: str,
) -> tuple[list[ShadowCandidateFact], str | None]:
    """Retrieve real candidate fact-edges for this episode. Cheap and read-only --
    safe to run inside the per-namespace ingest gate, and MUST run there (before the
    real add_episode call) so this episode's own about-to-be-created facts cannot leak
    into its own candidate pool.

    Returns (candidates, error). error is None on success (candidates may still be
    empty -- that's a legitimate "no signal" result, not a failure). error is set only
    when the retrieval itself broke, distinguishing "we looked and found nothing" from
    "we couldn't look" -- the caller needs that distinction for the status vocabulary.
    """
    if not namespace or not episode_body.strip():
        return [], None

    try:
        entity_uuids, retrieval_sources = await _retrieve_candidate_entity_uuids(
            graphiti_client, namespace=namespace, episode_body=episode_body,
        )
    except Exception as exc:
        logger.debug("Shadow candidate entity lookup failed namespace=%s", namespace, exc_info=True)
        return [], f"candidate_entity_lookup_failed: {exc}"

    if not entity_uuids:
        return [], None

    try:
        # fetch_candidate_fact_edges is synchronous (Neo4jRepository.execute is blocking) --
        # this runs inside the per-namespace ingest gate, so a blocking call here stalls the
        # WHOLE event loop, not just this namespace. to_thread keeps it off the loop.
        rows = await asyncio.to_thread(graph_adapter.fetch_candidate_fact_edges, list(entity_uuids))
    except Exception as exc:
        logger.debug("Shadow fact-edge fetch failed namespace=%s", namespace, exc_info=True)
        return [], f"fact_edge_fetch_failed: {exc}"

    candidates: list[ShadowCandidateFact] = []
    seen_fact_uuids: set[str] = set()
    for row in rows:
        fact_uuid = str(row.get("fact_uuid") or "").strip()
        if not fact_uuid or fact_uuid in seen_fact_uuids:
            # The undirected (n)-[r]-(m) pattern matches the same edge once per
            # endpoint that's in the candidate-uuid list -- a fact between two
            # independently-retrieved candidates (e.g. both "Rachel" and "Chicago"
            # matched separately) otherwise shows up twice. Dedup BEFORE the cap so
            # duplicates don't crowd out real distinct candidates within it.
            continue
        if len(candidates) >= _MAX_CANDIDATE_FACTS:
            break
        seen_fact_uuids.add(fact_uuid)
        source_uuid = str(row.get("source_uuid") or "")
        target_uuid = str(row.get("target_uuid") or "")
        # Union whichever side(s) were actually matched -- a fact-edge is very often
        # only matched via ONE endpoint (e.g. "Rachel" hit the literal-name lookup, but
        # "Chicago" as the other endpoint never did), so .get() on the unmatched side
        # must default to frozenset(), not None (frozenset | None raises TypeError).
        sources = (
            retrieval_sources.get(source_uuid, frozenset())
            | retrieval_sources.get(target_uuid, frozenset())
        )
        candidates.append(ShadowCandidateFact(
            fact_uuid=fact_uuid,
            fact_text=str(row.get("fact_text") or ""),
            source_uuid=source_uuid,
            source_name=str(row.get("source_name") or ""),
            target_uuid=target_uuid,
            target_name=str(row.get("target_name") or ""),
            valid_at=row.get("valid_at"),
            invalid_at=row.get("invalid_at"),
            created_at=row.get("created_at"),
            expired_at=row.get("expired_at"),
            retrieval_sources=sources or frozenset({"unknown"}),
        ))
    return candidates, None


async def _literal_name_candidates(
    graphiti_client: object, *, namespace: str, group_id: str, episode_body: str,
) -> set[str]:
    """Literal substring match -- same pattern as extraction_lab.py's
    _lookup_known_entities, against the raw graphiti-core Neo4j driver. Fails safe
    to set() on any error."""
    driver = getattr(getattr(getattr(graphiti_client, "client", None), "clients", None), "driver", None)
    if driver is None:
        return set()
    try:
        result = await driver.execute_query(
            """
            MATCH (n:Entity)
            WHERE n.group_id = $namespace AND n.scope = 'PERSISTENT'
            RETURN DISTINCT n.uuid AS uuid, n.name AS name
            """,
            params={"namespace": group_id},
        )
    except Exception:
        logger.debug("Shadow literal-name candidate lookup failed group_id=%s", group_id, exc_info=True)
        return set()

    message_lower = episode_body.lower()
    found: set[str] = set()
    for record in result.records:
        name = str(record.get("name") or "").strip()
        uuid = str(record.get("uuid") or "").strip()
        if uuid and len(name) >= _MIN_LITERAL_NAME_LEN and name.lower() in message_lower:
            found.add(uuid)
    return found


async def _semantic_search_candidates(
    graphiti_client: object, *, namespace: str, episode_body: str,
) -> set[str]:
    """Semantic search_scored -- same call correlation_service.check_correlation
    already makes post-extraction; here it's a pre-extraction candidate generator
    instead. Fails safe to set() on any error."""
    search_scored = getattr(graphiti_client, "search_scored", None)
    if search_scored is None:
        return set()
    try:
        similar = await search_scored(
            episode_body,
            num_results=_MAX_CANDIDATE_ENTITIES,
            group_ids=namespace_to_group_ids(namespace),
        )
    except Exception:
        logger.debug("Shadow semantic candidate search failed namespace=%s", namespace, exc_info=True)
        return set()
    return {str(other_uuid).strip() for other_uuid, _name, _score in similar if str(other_uuid or "").strip()}


async def _retrieve_candidate_entity_uuids(
    graphiti_client: object,
    *,
    namespace: str,
    episode_body: str,
) -> tuple[set[str], dict[str, frozenset[str]]]:
    """Literal-name substring match ∪ semantic search_scored, capped at
    _MAX_CANDIDATE_ENTITIES. Returns (entity_uuids, {uuid: {retrieval source tags}}).
    The two lookups are independent I/O and run concurrently via asyncio.gather --
    this runs inside the per-namespace ingest gate, so halving its wall-clock cost
    directly shortens how long other same-namespace episodes wait."""
    group_id = namespace_to_group_id(namespace)
    literal_uuids, semantic_uuids = await asyncio.gather(
        _literal_name_candidates(graphiti_client, namespace=namespace, group_id=group_id, episode_body=episode_body),
        _semantic_search_candidates(graphiti_client, namespace=namespace, episode_body=episode_body),
    )

    sources: dict[str, set[str]] = {}
    for uuid in literal_uuids:
        sources.setdefault(uuid, set()).add("literal_name")
    for uuid in semantic_uuids:
        sources.setdefault(uuid, set()).add("semantic_search")

    capped = set(list(literal_uuids | semantic_uuids)[:_MAX_CANDIDATE_ENTITIES])
    return capped, {u: frozenset(s) for u, s in sources.items() if u in capped}


# ---------------------------------------------------------------------------
# Phase 2 (outside the ingest gate, after the real extraction call has already been
# dispatched): the expensive part. Always returns a tagged prediction.
# ---------------------------------------------------------------------------

async def compose_shadow_prediction(
    llm: object,
    *,
    episode_uuid: str,
    namespace: str,
    episode_body: str,
    reference_time: str,
    candidates: list[ShadowCandidateFact],
    candidate_query_error: str | None,
    candidate_retrieval_ms: int = 0,
) -> ShadowCompositionPrediction:
    """Build the full shadow prediction from an already-captured candidate snapshot.
    Never raises; always returns a status-tagged ShadowCompositionPrediction. Timeout
    handling lives in run_shadow_composition_with_timeout(), which wraps this call --
    this function itself has no timeout logic, since asyncio.wait_for cancels the
    coroutine from the outside rather than letting it report its own timeout.

    candidate_retrieval_ms is passed in, not measured here: retrieval happened earlier,
    inside the ingest gate, via a separate call to snapshot_candidate_facts(). The
    caller times that call and forwards the duration so the trace's timing fields
    reflect where time was actually spent instead of a meaningless ~0ms measured
    around code that does no work.
    """
    if candidate_query_error is not None:
        return _empty_prediction(
            episode_uuid=episode_uuid, namespace=namespace,
            status=_STATUS_CANDIDATE_QUERY_FAILED,
            failure_stage="candidate_retrieval", error=candidate_query_error,
            reference_time=reference_time,
        )

    # Best-effort production facets, for context only -- never gates eligibility, never
    # conflated with the shadow_facet/shadow_state_family labels below.
    try:
        production_facets = tuple(
            f"{facet}={value}"
            for facet, value in sorted(derive_facets(content=episode_body, namespace=namespace).discrete_pairs())
        )
    except Exception:
        production_facets = ()

    if not candidates:
        return _empty_prediction(
            episode_uuid=episode_uuid, namespace=namespace,
            status=_STATUS_ABSTAINED_NO_CANDIDATES,
            reference_time=reference_time,
            production_facets=production_facets,
            candidate_retrieval_ms=candidate_retrieval_ms,
        )

    metadata_start = time.monotonic()
    classify = getattr(llm, "classify_shadow_context", None)
    if classify is None:
        return _empty_prediction(
            episode_uuid=episode_uuid, namespace=namespace,
            status=_STATUS_METADATA_GENERATION_FAILED,
            failure_stage="metadata_generation", error="llm collaborator has no classify_shadow_context",
            reference_time=reference_time, production_facets=production_facets,
            candidate_retrieval_ms=candidate_retrieval_ms, candidates=tuple(candidates),
        )

    try:
        raw = await classify(episode_body, [_candidate_payload(c) for c in candidates])
    except Exception as exc:
        return _empty_prediction(
            episode_uuid=episode_uuid, namespace=namespace,
            status=_STATUS_METADATA_GENERATION_FAILED,
            failure_stage="metadata_generation", error=str(exc),
            reference_time=reference_time, production_facets=production_facets,
            candidate_retrieval_ms=candidate_retrieval_ms, candidates=tuple(candidates),
        )
    metadata_generation_ms = int((time.monotonic() - metadata_start) * 1000)

    if raw is None:
        return _empty_prediction(
            episode_uuid=episode_uuid, namespace=namespace,
            status=_STATUS_METADATA_GENERATION_FAILED,
            failure_stage="metadata_generation", error="LLM call returned no content",
            reference_time=reference_time, production_facets=production_facets,
            candidate_retrieval_ms=candidate_retrieval_ms, candidates=tuple(candidates),
        )

    try:
        message_hypotheses, candidate_labels = _parse_shadow_grounded_response(raw)
    except ValueError as exc:
        return _empty_prediction(
            episode_uuid=episode_uuid, namespace=namespace,
            status=_STATUS_MALFORMED_LLM_RESPONSE,
            failure_stage="metadata_generation", error=str(exc),
            reference_time=reference_time, production_facets=production_facets,
            candidate_retrieval_ms=candidate_retrieval_ms, candidates=tuple(candidates),
            # The raw response is the single most useful diagnostic for a malformed-JSON
            # failure (was it truncated mid-object? did the model wrap it in prose?)
            # -- truncated to keep the telemetry row bounded.
            note=f"raw_llm_response[:500]={raw[:500]!r}",
        )

    selection_start = time.monotonic()
    selected_fact_uuid, abstention_reason, tie_break_fired, rejected = await _select_eligible_candidate(
        llm, episode_body=episode_body, reference_time=reference_time,
        candidates=candidates, message_hypotheses=message_hypotheses,
        candidate_labels=candidate_labels,
    )
    selection_ms = int((time.monotonic() - selection_start) * 1000)

    status = _STATUS_SELECTED if selected_fact_uuid is not None else (abstention_reason or _STATUS_ABSTAINED_NO_ELIGIBLE)

    return ShadowCompositionPrediction(
        episode_uuid=episode_uuid, namespace=namespace,
        status=status, failure_stage=None, error=None,
        candidates=tuple(candidates),
        message_hypotheses=tuple(message_hypotheses),
        candidate_labels=tuple(candidate_labels),
        production_facets=production_facets,
        rejected=tuple(rejected),
        selected_fact_uuid=selected_fact_uuid,
        abstention_reason=(None if selected_fact_uuid is not None else abstention_reason),
        llm_tie_breaker_fired=tie_break_fired,
        temporal_pivot=reference_time,
        candidate_retrieval_ms=candidate_retrieval_ms,
        metadata_generation_ms=metadata_generation_ms,
        selection_ms=selection_ms,
    )


async def run_shadow_composition_with_timeout(
    llm: object,
    *,
    episode_uuid: str,
    namespace: str,
    episode_body: str,
    reference_time: str,
    candidates: list[ShadowCandidateFact],
    candidate_query_error: str | None,
    candidate_retrieval_ms: int,
    timeout_s: float,
) -> ShadowCompositionPrediction:
    """Wraps compose_shadow_prediction() in an independent timeout. This is the actual
    entry point the background task (Stage 1 wiring in enrichment_steps.py) calls --
    asyncio.wait_for cancels the inner coroutine on timeout rather than letting it
    report its own status, so the "timed_out" status is constructed here, not inside
    compose_shadow_prediction()."""
    try:
        return await asyncio.wait_for(
            compose_shadow_prediction(
                llm, episode_uuid=episode_uuid, namespace=namespace,
                episode_body=episode_body, reference_time=reference_time,
                candidates=candidates, candidate_query_error=candidate_query_error,
                candidate_retrieval_ms=candidate_retrieval_ms,
            ),
            timeout=timeout_s,
        )
    except asyncio.TimeoutError:
        return _empty_prediction(
            episode_uuid=episode_uuid, namespace=namespace,
            status=_STATUS_TIMED_OUT,
            failure_stage="shadow_processing", error=f"exceeded {timeout_s:.1f}s budget",
            reference_time=reference_time, candidates=tuple(candidates),
            candidate_retrieval_ms=candidate_retrieval_ms,
        )
    except Exception as exc:  # last-resort safety net -- this must never propagate
        logger.warning("Shadow composition failed unexpectedly episode_id=%s", episode_uuid, exc_info=True)
        return _empty_prediction(
            episode_uuid=episode_uuid, namespace=namespace, candidates=tuple(candidates),
            candidate_retrieval_ms=candidate_retrieval_ms,
            status=_STATUS_METADATA_GENERATION_FAILED,
            failure_stage="unexpected", error=str(exc),
            reference_time=reference_time,
        )
