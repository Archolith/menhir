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
import json
import logging
import re
import time
from dataclasses import dataclass

from menhir.domain.facet_derivation import derive_facets
from menhir.domain.namespace import namespace_to_group_id, namespace_to_group_ids
from menhir.domain.self_identity import self_uuid_for_namespace
from menhir.domain.temporal import FactTemporal, TemporalQuery, matches_query
from menhir.infrastructure.self_binding import SelfBindMode, resolve_bind_mode

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

_STATUS_SELECTED = "selected"
_STATUS_ABSTAINED_NO_CANDIDATES = "abstained_no_candidates"
_STATUS_ABSTAINED_NO_ELIGIBLE = "abstained_no_eligible_candidates"
_STATUS_ABSTAINED_TIE = "abstained_tie"
_STATUS_CANDIDATE_QUERY_FAILED = "candidate_query_failed"
_STATUS_METADATA_GENERATION_FAILED = "metadata_generation_failed"
_STATUS_MALFORMED_LLM_RESPONSE = "malformed_llm_response"
_STATUS_TIMED_OUT = "timed_out"


# ---------------------------------------------------------------------------
# Dataclasses — all frozen; ShadowCompositionPrediction and
# ExtractionCompositionShadowTrace are each constructed exactly once, with final data,
# never mutated after the fact (see the plan's point 3 for why: a frozen dataclass that
# claims to be filled in later is a contradiction, not a design).
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ShadowCandidateFact:
    """One real fact-edge from the graph, at edge (not entity) granularity."""

    fact_uuid: str
    fact_text: str
    source_uuid: str
    source_name: str
    target_uuid: str
    target_name: str
    valid_at: str | None
    invalid_at: str | None
    created_at: str | None
    expired_at: str | None
    retrieval_sources: frozenset[str]  # {"literal_name", "semantic_search"}
    semantic_score: float | None = None


@dataclass(frozen=True)
class ShadowRankedHypothesis:
    """Message-side: what the episode itself implies, up to 2 ranked guesses."""

    shadow_facet: str
    shadow_state_family: str
    confidence: float


@dataclass(frozen=True)
class ShadowCandidateLabels:
    """Candidate-side: labels grounded in ONE real candidate fact's own text."""

    fact_uuid: str
    shadow_facet: str | None
    shadow_state_family: str | None
    shadow_scope: str | None


@dataclass(frozen=True)
class ShadowRejection:
    fact_uuid: str
    reason: str


@dataclass(frozen=True)
class ShadowCompositionPrediction:
    """Everything knowable BEFORE the real extraction call returns. Complete and
    immutable at construction. status is always one of the _STATUS_* constants above;
    when status != "selected"/"abstained_*", failure_stage/error are populated instead
    of candidates/hypotheses (which may be empty but are never fabricated)."""

    episode_uuid: str
    namespace: str
    status: str
    failure_stage: str | None
    error: str | None
    candidates: tuple[ShadowCandidateFact, ...]
    message_hypotheses: tuple[ShadowRankedHypothesis, ...]
    candidate_labels: tuple[ShadowCandidateLabels, ...]
    production_facets: tuple[str, ...]
    rejected: tuple[ShadowRejection, ...]
    selected_fact_uuid: str | None
    abstention_reason: str | None
    llm_tie_breaker_fired: bool
    temporal_query: str = TemporalQuery.AS_KNOWN_AT.value
    temporal_pivot: str | None = None
    candidate_retrieval_ms: int = 0
    metadata_generation_ms: int = 0
    selection_ms: int = 0
    note: str = ""


@dataclass(frozen=True)
class ExtractionCompositionShadowTrace:
    """Prediction + real outcome, combined once in build_shadow_trace()."""

    prediction: ShadowCompositionPrediction
    production_extracted_node_names: tuple[str, ...]
    production_extracted_facts: tuple[str, ...]
    shadow_total_ms: int


def _empty_prediction(
    *,
    episode_uuid: str,
    namespace: str,
    status: str,
    failure_stage: str | None = None,
    error: str | None = None,
    reference_time: str | None = None,
    production_facets: tuple[str, ...] = (),
    candidate_retrieval_ms: int = 0,
    candidates: tuple[ShadowCandidateFact, ...] = (),
    note: str = "",
) -> ShadowCompositionPrediction:
    """Build a prediction for any early-exit path (failure or trivial abstention).
    Centralizes the "always return a tagged result, never None" contract -- every
    early-exit path in compose_shadow_prediction (including the "no candidates"
    abstention) goes through here rather than hand-rolling the same ~10-field
    construction, so a future field addition only needs updating in one place.

    candidates defaults to () (the true "no candidates" cases), but every failure
    path that occurs AFTER real candidates were already retrieved (metadata
    generation failure, malformed LLM response, timeout) MUST pass the real
    candidates through -- silently blanking them on failure is exactly the kind of
    hidden-failure-mode this whole observability stage exists to prevent. A
    malformed_llm_response trace with an empty candidate list is undiagnosable:
    you can't tell whether the LLM saw reasonable input and produced garbage
    output, or never got meaningful input in the first place."""
    return ShadowCompositionPrediction(
        episode_uuid=episode_uuid,
        namespace=namespace,
        status=status,
        failure_stage=failure_stage,
        error=error,
        candidates=candidates,
        message_hypotheses=(),
        candidate_labels=(),
        production_facets=production_facets,
        rejected=(),
        selected_fact_uuid=None,
        abstention_reason=(status if status.startswith("abstained_") else None),
        llm_tie_breaker_fired=False,
        temporal_pivot=reference_time,
        candidate_retrieval_ms=candidate_retrieval_ms,
        note=note,
    )


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
    protect_self = (
        resolve_bind_mode(getattr(graph_adapter, "canonical_self_binding_mode", "off"))
        is SelfBindMode.ENFORCE
    )
    canonical_self_uuid = self_uuid_for_namespace(namespace) if protect_self else ""
    verifier = getattr(graphiti_client, "canonical_self_payload_is_authorized", None)
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
        source_uuid = str(row.get("source_uuid") or "")
        target_uuid = str(row.get("target_uuid") or "")
        if canonical_self_uuid in {source_uuid, target_uuid} and not (
            callable(verifier)
            and verifier(
                row.get("self_authority_payload_json"),
                expected_self_uuid=canonical_self_uuid,
                source_node_uuid=row.get("edge_source_uuid"),
                target_node_uuid=row.get("edge_target_uuid"),
                counterpart_name=(
                    row.get("edge_target_name")
                    if row.get("edge_source_uuid") == canonical_self_uuid
                    else row.get("edge_source_name")
                ),
                counterpart_labels=(
                    row.get("edge_target_labels")
                    if row.get("edge_source_uuid") == canonical_self_uuid
                    else row.get("edge_source_labels")
                ),
                predicate=row.get("predicate"),
                fact=row.get("fact_text"),
                valid_at=row.get("valid_at"),
                invalid_at=row.get("invalid_at"),
                expired_at=row.get("expired_at"),
            )
        ):
            continue
        seen_fact_uuids.add(fact_uuid)
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


def _candidate_payload(c: ShadowCandidateFact) -> dict[str, str]:
    return {
        "fact_uuid": c.fact_uuid, "fact_text": c.fact_text,
        "source_name": c.source_name, "target_name": c.target_name,
    }


def _extract_json_object(raw: str) -> dict:
    cleaned = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("LLM response did not contain a JSON object")
    return json.loads(cleaned[start:end + 1])


def _parse_shadow_grounded_response(
    raw: str,
) -> tuple[list[ShadowRankedHypothesis], list[ShadowCandidateLabels]]:
    parsed = _extract_json_object(raw)

    hyps_raw = parsed.get("message_hypotheses")
    hypotheses: list[ShadowRankedHypothesis] = []
    if isinstance(hyps_raw, list):
        for h in hyps_raw:
            if not isinstance(h, dict):
                continue
            facet, family = h.get("shadow_facet"), h.get("shadow_state_family")
            if facet and family:
                try:
                    confidence = float(h.get("confidence", 0.0))
                except (TypeError, ValueError):
                    confidence = 0.0
                hypotheses.append(ShadowRankedHypothesis(str(facet), str(family), confidence))

    labels_raw = parsed.get("candidate_labels")
    labels: list[ShadowCandidateLabels] = []
    if isinstance(labels_raw, list):
        for entry in labels_raw:
            if not isinstance(entry, dict) or not entry.get("fact_uuid"):
                continue
            labels.append(ShadowCandidateLabels(
                fact_uuid=str(entry["fact_uuid"]),
                shadow_facet=(str(entry["shadow_facet"]) if entry.get("shadow_facet") else None),
                shadow_state_family=(str(entry["shadow_state_family"]) if entry.get("shadow_state_family") else None),
                shadow_scope=(str(entry["shadow_scope"]) if entry.get("shadow_scope") else None),
            ))

    if not hypotheses and not labels:
        raise ValueError("LLM response had neither message_hypotheses nor candidate_labels")
    return hypotheses, labels


def _label_matches_hypothesis(label: ShadowCandidateLabels, hyp: ShadowRankedHypothesis) -> bool:
    if label.shadow_facet is None or label.shadow_state_family is None:
        return False
    return (
        label.shadow_facet.strip().lower() == hyp.shadow_facet.strip().lower()
        and label.shadow_state_family.strip().lower() == hyp.shadow_state_family.strip().lower()
    )


async def _select_eligible_candidate(
    llm: object,
    *,
    episode_body: str,
    reference_time: str,
    candidates: list[ShadowCandidateFact],
    message_hypotheses: list[ShadowRankedHypothesis],
    candidate_labels: list[ShadowCandidateLabels],
) -> tuple[str | None, str | None, bool, list[ShadowRejection]]:
    """Deterministic eligibility filter (temporal + shadow-label match against the top
    hypothesis), LLM tie-break only when 2+ candidates survive. Returns
    (selected_fact_uuid, abstention_reason, llm_tie_breaker_fired, rejected)."""
    rejected: list[ShadowRejection] = []

    if not message_hypotheses:
        for c in candidates:
            rejected.append(ShadowRejection(c.fact_uuid, "no_message_hypothesis"))
        return None, _STATUS_ABSTAINED_NO_ELIGIBLE, False, rejected

    top_hypothesis = message_hypotheses[0]
    labels_by_uuid = {label.fact_uuid: label for label in candidate_labels}

    survivors: list[ShadowCandidateFact] = []
    for c in candidates:
        temporal = FactTemporal(
            valid_at=c.valid_at, invalid_at=c.invalid_at,
            created_at=c.created_at, expired_at=c.expired_at,
        )
        if not matches_query(temporal, TemporalQuery.AS_KNOWN_AT, as_of=reference_time):
            rejected.append(ShadowRejection(c.fact_uuid, "not_known_at_reference_time"))
            continue
        label = labels_by_uuid.get(c.fact_uuid)
        if label is None:
            rejected.append(ShadowRejection(c.fact_uuid, "no_matching_label"))
            continue
        if not _label_matches_hypothesis(label, top_hypothesis):
            rejected.append(ShadowRejection(c.fact_uuid, "shadow_label_mismatch"))
            continue
        survivors.append(c)

    if not survivors:
        return None, _STATUS_ABSTAINED_NO_ELIGIBLE, False, rejected
    if len(survivors) == 1:
        return survivors[0].fact_uuid, None, False, rejected

    # 2+ survivors: genuine tie -- consult the LLM tie-breaker rather than picking
    # arbitrarily (Phase 5 item 4's finding: the deterministic path always picks
    # SOMETHING under a tie, which is exactly the case this stage must not repeat).
    break_tie = getattr(llm, "break_shadow_tie", None)
    if break_tie is None:
        return None, _STATUS_ABSTAINED_TIE, False, rejected
    try:
        raw = await break_tie(episode_body, [_candidate_payload(c) for c in survivors])
    except Exception:
        return None, _STATUS_ABSTAINED_TIE, True, rejected
    if raw is None:
        return None, _STATUS_ABSTAINED_TIE, True, rejected
    try:
        parsed = _extract_json_object(raw)
    except ValueError:
        return None, _STATUS_ABSTAINED_TIE, True, rejected

    picked = parsed.get("selected_fact_uuid")
    survivor_uuids = {c.fact_uuid for c in survivors}
    if picked and str(picked) in survivor_uuids:
        return str(picked), None, True, rejected
    return None, _STATUS_ABSTAINED_TIE, True, rejected


# ---------------------------------------------------------------------------
# Combine prediction + real extraction outcome into the final frozen trace.
# ---------------------------------------------------------------------------

def build_shadow_trace(
    prediction: ShadowCompositionPrediction,
    graphiti_result: object,
    *,
    shadow_total_ms: int,
) -> ExtractionCompositionShadowTrace:
    """One-shot construction combining the already-frozen prediction with the real
    extraction outcome. Never mutates prediction; never called more than once per
    episode."""
    node_names: tuple[str, ...] = ()
    fact_texts: tuple[str, ...] = ()
    try:
        nodes = getattr(graphiti_result, "nodes", None) or []
        node_names = tuple(str(getattr(n, "name", "") or "") for n in nodes if getattr(n, "name", None))
        edges = getattr(graphiti_result, "edges", None) or []
        fact_texts = tuple(str(getattr(e, "fact", "") or "") for e in edges if getattr(e, "fact", None))
    except Exception:
        logger.debug("Failed to read production extraction result for shadow trace", exc_info=True)

    return ExtractionCompositionShadowTrace(
        prediction=prediction,
        production_extracted_node_names=node_names,
        production_extracted_facts=fact_texts,
        shadow_total_ms=shadow_total_ms,
    )


def shadow_trace_to_details(trace: ExtractionCompositionShadowTrace) -> dict[str, object]:
    """Flatten the trace into a JSON-serializable dict for record_lifecycle_event's
    details_json column (dataclasses.asdict handles nested frozen dataclasses and
    tuples/frozensets need explicit list conversion for JSON)."""
    p = trace.prediction
    return {
        "episode_uuid": p.episode_uuid,
        "namespace": p.namespace,
        "status": p.status,
        "failure_stage": p.failure_stage,
        "error": p.error,
        "candidates": [
            {
                "fact_uuid": c.fact_uuid, "fact_text": c.fact_text,
                "source_uuid": c.source_uuid, "source_name": c.source_name,
                "target_uuid": c.target_uuid, "target_name": c.target_name,
                "valid_at": c.valid_at, "invalid_at": c.invalid_at,
                "created_at": c.created_at, "expired_at": c.expired_at,
                "retrieval_sources": sorted(c.retrieval_sources),
                "semantic_score": c.semantic_score,
            }
            for c in p.candidates
        ],
        "message_hypotheses": [
            {"shadow_facet": h.shadow_facet, "shadow_state_family": h.shadow_state_family, "confidence": h.confidence}
            for h in p.message_hypotheses
        ],
        "candidate_labels": [
            {
                "fact_uuid": lb.fact_uuid, "shadow_facet": lb.shadow_facet,
                "shadow_state_family": lb.shadow_state_family, "shadow_scope": lb.shadow_scope,
            }
            for lb in p.candidate_labels
        ],
        "production_facets": list(p.production_facets),
        "rejected": [{"fact_uuid": r.fact_uuid, "reason": r.reason} for r in p.rejected],
        "selected_fact_uuid": p.selected_fact_uuid,
        "abstention_reason": p.abstention_reason,
        "llm_tie_breaker_fired": p.llm_tie_breaker_fired,
        "temporal_query": p.temporal_query,
        "temporal_pivot": p.temporal_pivot,
        "candidate_retrieval_ms": p.candidate_retrieval_ms,
        "metadata_generation_ms": p.metadata_generation_ms,
        "selection_ms": p.selection_ms,
        "note": p.note,
        "production_extracted_node_names": list(trace.production_extracted_node_names),
        "production_extracted_facts": list(trace.production_extracted_facts),
        "shadow_total_ms": trace.shadow_total_ms,
    }
