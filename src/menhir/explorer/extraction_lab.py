"""Read-only extraction harness experiments for the Explorer Extraction Lab.

Phase 0 (instrumentation): Replicate production extraction pipeline in a testable,
multi-arm format to measure prompt/schema effects on entity extraction recall/precision.

Fidelity contract: identical to production extraction except for the one tuning knob
under test (prompt_variant, model, or context_episode_count). See
.agent/plans/menhir-belief-supersession-code-mapped-plan.md phase-0 spec for details.
"""

from __future__ import annotations

import asyncio
import logging
from time import perf_counter
from typing import Callable

from menhir.explorer.extraction_lab_models import (  # noqa: F401
    PROMPT_VARIANTS,
    EpisodeFixture,
    ExtractionGoldScore,
    ExtractionLabArm,
    ExtractionLabArmResult,
    ExtractionLabRequest,
    ExtractionLabRunPayload,
    ExtractionLabTuning,
    ExtractionResult,
    GoldExtraction,
)
from menhir.explorer.extraction_lab_prompts import (  # noqa: F401
    _CURRENT_MESSAGE_CLOSE_MARKER,
    _MINIMAL_RECALL_PATCH_REPLACEMENT,
    _VARIANT_APPEND_SECTIONS,
    _WHEN_IN_DOUBT_SENTENCE,
    _apply_extraction_patches,
    _known_entities_section,
    _retrieved_context_section,
)
from menhir.explorer.extraction_lab_scoring import (  # noqa: F401
    _FUZZY_MATCH_SYSTEM_PROMPT,
    _compute_set_metrics,
    _fuzzy_match_prompt,
    _fuzzy_matched_counts,
    _normalize_text,
    _parse_fuzzy_matches,
    _score_extraction,
    _scored_set_comparison,
    _text_matches,
)

logger = logging.getLogger(__name__)

#: The prompt patch is process-global, so the patched window is a critical section:
#: two concurrent lab requests would otherwise run arms under each other's prompts
#: and silently mis-measure.
_PROMPT_PATCH_LOCK = asyncio.Lock()


async def _run_extraction_arm(
    arm: ExtractionLabArm,
    request: ExtractionLabRequest,
    *,
    llm_backend: object | None = None,
) -> ExtractionLabArmResult:
    """Run a single extraction arm: apply patches, extract, score."""
    started = perf_counter()
    restore_prompt: Callable[[], None] | None = None
    _prompt_patch_lock_held = False

    try:
        # Import here to ensure patches are applied at construction time if needed
        from menhir.config import MemorySettings
        from menhir.infrastructure.graphiti_client import GraphitiClient
        from graphiti_core.nodes import EpisodicNode, EpisodeType
        from graphiti_core.utils.datetime_utils import utc_now
        from graphiti_core.utils.maintenance.node_operations import (
            extract_nodes,
            resolve_extracted_nodes,
        )
        from graphiti_core.utils.maintenance.edge_operations import (
            extract_edges,
            resolve_extracted_edges,
        )
        from graphiti_core.utils.bulk_utils import resolve_edge_pointers
        from graphiti_core.utils.maintenance.combined_extraction import (
            extract_nodes_and_edges as extract_combined,
        )

        # Build GraphitiClient via production path (patches are applied here).
        # MemorySettings.from_env(), NOT the bare constructor -- every other call site
        # in the codebase uses .from_env() to actually read the runtime environment
        # (provider, API keys, Neo4j URI, model). The bare constructor silently ignores
        # the environment and falls back to dataclass defaults -- found the hard way
        # during Phase 1's first live run: env-var provider/model overrides were
        # silently no-ops and the harness tried to reach a Neo4j that was never
        # configured, exactly the kind of harness/production divergence the fidelity
        # contract exists to catch.
        # CF-107: `from_settings` reads settings and builds clients synchronously; keep it off
        # the shared event loop (up to 16 arms per request).
        settings = await asyncio.to_thread(MemorySettings.from_env)
        graphiti_client = await asyncio.to_thread(GraphitiClient.from_settings, settings)
        clients = graphiti_client.client.clients

        # Apply model/temperature overrides. Safe to mutate directly (unlike the prompt
        # patch below) because from_settings() built a fresh client for THIS arm alone --
        # no shared global state, so no restore/race concerns. model=None means "use
        # production's resolved default", per the fidelity contract (don't hardcode a
        # model in the harness that could drift from the real default).
        if arm.tuning.model:
            clients.llm_client.config.model = arm.tuning.model
        clients.llm_client.temperature = arm.tuning.temperature

        # Phase 2: candidate lookup against the real backing namespace, independent of
        # context_episode_count. No-op (empty list) unless both the arm opts in AND the
        # request carries a real source_namespace -- synthetic fixtures correctly get no
        # signal here, matching _lookup_known_entities' documented fail-safe behavior.
        known_entities: list[str] = []
        if arm.tuning.forced_known_entities is not None:
            # Ablation override -- bypass the DB lookup entirely (see field docstring).
            known_entities = list(arm.tuning.forced_known_entities)
        elif arm.tuning.enable_candidate_lookup and request.source_namespace:
            known_entities = await _lookup_known_entities(
                clients, request.source_namespace, request.current_message
            )

        # Apply prompt variant + candidate-lookup monkey-patch if needed. prompt_library
        # is process-global shared state (see _apply_extraction_patches docstring) --
        # restore is MANDATORY even on exception, or a later arm/request would silently
        # inherit this arm's variant.
        #
        # Serialize the ENTIRE patched window with _PROMPT_PATCH_LOCK: the patch, every
        # awaited extraction call below, and the restore in the finally. The lock spans
        # from here (just before the patch) to the finally's restore_prompt() -- that is
        # the narrowest span that still contains the whole window, and it leaves the
        # candidate lookup above (no shared global state) outside the critical section.
        await _PROMPT_PATCH_LOCK.acquire()
        _prompt_patch_lock_held = True

        restore_prompt = _apply_extraction_patches(
            arm.tuning.prompt_variant, known_entities, arm.tuning.retrieved_context
        )

        # Build isolated EpisodicNode for current message
        now = utc_now()
        current_episode = EpisodicNode(
            name="extraction-lab-test",
            group_id="extraction-lab",
            labels=[],
            source=EpisodeType.message,
            content=request.current_message,
            source_description="extraction_lab",
            created_at=now,
            valid_at=now,
        )

        # Build previous episodes list (respecting context_episode_count limit)
        previous_episodes = []
        for episode_fixture in request.previous_episodes[-arm.tuning.context_episode_count:]:
            prev_ep = EpisodicNode(
                name="extraction-lab-previous",
                group_id="extraction-lab",
                labels=[],
                source=EpisodeType.message,
                content=episode_fixture.text,
                source_description="extraction_lab",
                created_at=episode_fixture.created_at,
                valid_at=episode_fixture.created_at,
            )
            previous_episodes.append(prev_ep)

        # Step 1: extract nodes (and, for the fallback candidate, edges in the same
        # typed response). Graphiti 0.29 ships this combined path but does not use it
        # from single-episode add_episode even though its own docstring says it avoids
        # orphaned nodes by letting the model see the proposition and entities together.
        combined_edges = None
        if arm.tuning.prompt_variant == "combined_extraction":
            extracted_nodes, combined_edges, index_map = await extract_combined(
                clients, current_episode, previous_episodes, None, None, None, None, None
            )
        else:
            extracted_nodes, index_map = await extract_nodes(
                clients, current_episode, previous_episodes, None, None, None
            )

        # Step 2: resolve_extracted_nodes
        resolved_nodes, uuid_map, duplicates = await resolve_extracted_nodes(
            clients, extracted_nodes, current_episode, previous_episodes, None,
        )

        # Step 3: extract_edges
        edge_type_map = {("Entity", "Entity"): []}
        extracted_edges = combined_edges
        if extracted_edges is None:
            extracted_edges = await extract_edges(
                clients,
                current_episode,
                extracted_nodes,
                previous_episodes,
                edge_type_map,
                group_id="extraction-lab",
            )

        # Step 4: resolve_extracted_edges
        edges = resolve_edge_pointers(extracted_edges, uuid_map)
        resolved_edges, invalidated_edges, new_edges = await resolve_extracted_edges(
            clients, edges, current_episode, resolved_nodes, {}, edge_type_map,
        )

        # Serialize results
        mentions = [
            {
                "text": node.name,
                "labels": node.labels or [],
                "uuid": str(node.uuid),
            }
            for node in resolved_nodes
        ]

        propositions = [
            {
                "fact": edge.fact,
                "source_uuid": str(edge.source_node_uuid),
                "target_uuid": str(edge.target_node_uuid),
                "uuid": str(edge.uuid),
            }
            for edge in (resolved_edges + new_edges)
        ]

        extraction = ExtractionResult(
            mentions=mentions,
            propositions=propositions,
        )

        # Score against gold
        gold_scores = await _score_extraction(extraction, request.gold, llm_backend=llm_backend)

        # Close client
        await graphiti_client.client.close()

        return ExtractionLabArmResult(
            id=arm.id,
            label=arm.label,
            ok=True,
            enabled=arm.enabled,
            elapsed_ms=round((perf_counter() - started) * 1000, 2),
            tuning=arm.tuning.model_dump(mode="json"),
            extraction=extraction,
            gold_scores=gold_scores,
            known_entities_used=known_entities,
        )

    except Exception as exc:
        return ExtractionLabArmResult(
            id=arm.id,
            label=arm.label,
            ok=False,
            enabled=arm.enabled,
            elapsed_ms=round((perf_counter() - started) * 1000, 2),
            tuning=arm.tuning.model_dump(mode="json"),
            error=f"{type(exc).__name__}: {exc}",
        )
    finally:
        # Mandatory even on the success path above (which already returned) -- this
        # covers every exception exit. prompt_library.extract_nodes.extract_message is
        # process-global; leaving a variant patched after this arm fails would silently
        # corrupt every subsequent arm/request until the process restarts. This finally
        # covers SEQUENTIAL reuse (one arm after another); _PROMPT_PATCH_LOCK covers
        # CONCURRENT reuse (two in-process lab requests on the same event loop), so
        # neither can be deleted as redundant. The lock is released here too, after the
        # restore, so an arm that raised still hands the critical section back.
        if restore_prompt is not None:
            restore_prompt()
        if _prompt_patch_lock_held:
            _PROMPT_PATCH_LOCK.release()




async def _lookup_known_entities(
    clients: object,
    namespace: str | None,
    message: str,
) -> list[str]:
    """Phase 2's candidate lookup: existing PERSISTENT entity names in `namespace`
    (a real graphiti-core group_id, e.g. "lme-830ce83f") that appear literally in
    `message` -- independent of RELEVANT_SCHEMA_LIMIT / context_episode_count, which
    is the entire point (see the plan's Phase 2 spec). Deliberately the cheapest
    possible signal for a first test: exact case-insensitive substring match, no
    embedding similarity -- the belief-supersession plan's own "Candidate Retrieval"
    philosophy is "intentionally over-include, let the downstream step filter," and
    that downstream filter here is the extractor's own judgment (the KNOWN ENTITIES
    block states a fact, not a command).

    Fails safe to [] on any error, missing namespace, or missing driver -- a failed
    lookup must never crash extraction, and an empty result is exactly the
    "no additional signal" case every other code path here already handles.
    """
    if not namespace:
        return []
    driver = getattr(clients, "driver", None)
    if driver is None:
        return []
    try:
        # graphiti_core.driver.neo4j_driver.Neo4jDriver.execute_query(cypher, **kwargs)
        # pops kwargs["params"] as the query parameter dict and auto-fills database_
        # from the driver's own bound database -- NOT a bare namespace=... kwarg (that
        # would be forwarded straight to the underlying neo4j driver's execute_query,
        # which does not accept arbitrary query params that way).
        result = await driver.execute_query(
            """
            MATCH (n:Entity)
            WHERE n.group_id = $namespace AND n.scope = 'PERSISTENT'
            RETURN DISTINCT n.name AS name
            """,
            params={"namespace": namespace},
        )
        records = result.records
    except Exception:
        logger.warning("Extraction Lab candidate lookup failed for namespace=%s", namespace, exc_info=True)
        return []

    message_lower = message.lower()
    matched: list[str] = []
    for record in records:
        name = str(record.get("name") or "").strip()
        # Skip short/generic names (e.g. "user", "it") -- a 2-3 char substring match
        # against arbitrary message text is noise, not signal, and inflates the
        # unsupported_inference_rate without helping recall.
        if len(name) >= 3 and name.lower() in message_lower:
            matched.append(name)
    return matched




async def run_extraction_lab(
    request: ExtractionLabRequest,
) -> ExtractionLabRunPayload:
    """Run all enabled extraction arms.

    Deliberately SEQUENTIAL, unlike recall_lab.py's run_recall_lab (which fans arms out
    concurrently via asyncio.gather). Recall Lab's arms are safe to run concurrently
    because RetrievalTuningConfig is passed as a plain function argument -- no shared
    mutable state. Extraction arms are not: _apply_extraction_patches patches
    graphiti_core.prompts.prompt_library, a process-global object, for the duration of
    one arm's extraction call. Running arms concurrently would let one arm's patch
    apply while another arm's extraction call is still in flight, silently corrupting
    both results with a race that would not show up as an error -- it would just look
    like a possibly-wrong extraction with no signal that anything went wrong. Sequential
    execution is the only correct mode here; it costs latency (arms x LLM call time
    instead of max(arms)), which is an acceptable tradeoff for a correctness-first lab
    tool that is not on any user-facing request path.

    Builds one shared chat backend for gold-scoring's fuzzy-match tier (see
    _score_extraction), reused across every arm in this run rather than rebuilt per
    arm -- this is scoring infrastructure, not part of the per-arm extraction call, so
    it sits outside the fidelity contract entirely. Falls back to exact-match-only
    scoring (llm_backend=None) if the backend can't be built; never fatal.
    """
    enabled = [arm for arm in request.arms if arm.enabled]

    llm_backend: object | None = None
    try:
        from menhir.config import MemorySettings
        from menhir.infrastructure.providers import build_chat_backend

        llm_backend = build_chat_backend(MemorySettings.from_env())
    except Exception:
        logger.warning(
            "Extraction Lab: could not build a scoring LLM backend; "
            "falling back to exact-match-only gold scoring",
            exc_info=True,
        )

    arm_results = [
        await _run_extraction_arm(arm, request, llm_backend=llm_backend) for arm in enabled
    ]

    return ExtractionLabRunPayload(
        current_message=request.current_message,
        arms=arm_results,
    )
