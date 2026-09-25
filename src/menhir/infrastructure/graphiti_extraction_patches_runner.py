"""The single-episode combined-extraction pass: repair, endpoint correction, and binding order.

Owns ``_run_graphiti_combined_extraction`` — the replacement extractor body that sequences the
first pass, the relationless repair, the undeclared-endpoint correction, and the binding decision.
Extracted from ``graphiti_extraction_patches`` (which re-exports it): the patch seam globals stay
on the facade module, so the two facade-bound helpers are resolved through it at call time.
"""

from __future__ import annotations

import logging
from typing import Any

from menhir.domain.self_identity import SUBJECT_ENDPOINT_MARKER_PREFIX
from menhir.infrastructure.graphiti_extraction_patches_prompts import (
    _RELATIONLESS_REPAIR_CONTEXT_INSTRUCTIONS,
    _combine_extraction_instructions,
    _load_relationless_repair_context,
    _needs_relationless_repair,
    _relation_completeness_instructions,
    _relationless_repair_instructions,
    _relationless_repair_previous_episodes,
    _subject_endpoint_correction_instructions,
    _subject_endpoint_instructions,
    _unresolved_author_aliases,
)
from menhir.infrastructure.graphiti_extraction_patches_receipt import _extraction_receipt
from menhir.infrastructure.self_binding import InvalidSelfSubjectDeclarationError

logger = logging.getLogger(__name__)


async def _run_graphiti_combined_extraction(
    clients: Any,
    episode: Any,
    previous_episodes: list[Any],
    entity_types: Any,
    excluded_entity_types: Any,
    custom_extraction_instructions: str | None,
) -> tuple[list[Any], list[Any], dict[str, list[int]]]:
    # Resolved at patch time (see `_patch_graphiti_combined_extraction`) so the patch's own
    # ImportError guard covers this dependency. Importing it here instead put the replacement
    # function's real dependency outside the guard: the patch reported success and every
    # subsequent add_episode raised. The helper lives on the facade module (its module globals
    # are the patch state and the tests' monkeypatch seams), so resolve both through it lazily.
    from menhir.infrastructure.graphiti_extraction_patches import (
        _record_self_binding,
        _resolve_combined_extractor,
    )

    extract_nodes_and_edges = _resolve_combined_extractor()

    receipt = _extraction_receipt.get()
    if receipt is not None:
        receipt.graphiti_episode_uuid = str(getattr(episode, "uuid", "") or "").strip()
        receipt.previous_episode_texts = tuple(
            content
            for item in (previous_episodes or [])
            if isinstance((content := getattr(item, "content", None)), str)
            and content.strip()
        )

    declared_endpoint = receipt.self_subject_endpoint if receipt is not None else None
    endpoint = declared_endpoint  # Transport exists regardless of grammar or model output.
    if declared_endpoint is not None:
        # Eligibility is rare and enforce-only.  Pay the bounded graph read up front so a marker
        # collision in repair context is rejected before even the first model dispatch; the same
        # cached context is reused if relationless repair is actually needed.
        if receipt.relationless_repair_context_loader is not None:
            receipt.relationless_repair_context_texts = _load_relationless_repair_context(
                receipt
            )
        collision_texts = (
            receipt.episode_text,
            *receipt.previous_episode_texts,
            *receipt.relationless_repair_context_texts,
        )
        if any(
            SUBJECT_ENDPOINT_MARKER_PREFIX.casefold() in text.casefold()
            for text in collision_texts
        ):
            raise InvalidSelfSubjectDeclarationError(
                "reserved self-subject marker prefix occurs in extraction text or context"
            )
    endpoint_instructions = _subject_endpoint_instructions(endpoint)

    effective_instructions = _combine_extraction_instructions(
        custom_extraction_instructions,
        _relation_completeness_instructions(endpoint, receipt.episode_text),
        endpoint_instructions,
    )
    nodes, edges, index_map = await extract_nodes_and_edges(
        clients,
        episode,
        previous_episodes,
        entity_types=entity_types,
        excluded_entity_types=excluded_entity_types,
        custom_extraction_instructions=effective_instructions,
    )
    if _needs_relationless_repair(receipt, edges):
        assert receipt is not None  # narrowed by _needs_relationless_repair
        receipt.relationless_repair_attempted = True
        receipt.relationless_initial_entity_count = receipt.raw_entity_count
        receipt.relationless_initial_edge_count = receipt.raw_edge_count
        if not receipt.relationless_repair_context_texts:
            receipt.relationless_repair_context_texts = _load_relationless_repair_context(
                receipt
            )
        if declared_endpoint is not None and any(
            SUBJECT_ENDPOINT_MARKER_PREFIX.casefold() in text.casefold()
            for text in receipt.relationless_repair_context_texts
        ):
            raise InvalidSelfSubjectDeclarationError(
                "reserved self-subject marker prefix occurs in repair context"
            )
        logger.warning(
            "Relationless combined extraction; running one corrective retry "
            "episode_id=%s raw_entities=%d raw_edges=%d source=%s adjacent_context_turns=%d",
            receipt.episode_key,
            receipt.raw_entity_count,
            receipt.raw_edge_count,
            receipt.source_description,
            len(receipt.relationless_repair_context_texts),
        )
        repair_instructions = _combine_extraction_instructions(
            effective_instructions,
            _relationless_repair_instructions(endpoint, receipt.episode_text),
            (
                _RELATIONLESS_REPAIR_CONTEXT_INSTRUCTIONS
                if receipt.relationless_repair_context_texts
                else None
            ),
            endpoint_instructions,
        )
        repair_previous_episodes = _relationless_repair_previous_episodes(
            episode,
            previous_episodes,
            receipt.relationless_repair_context_texts,
        )
        nodes, edges, index_map = await extract_nodes_and_edges(
            clients,
            episode,
            repair_previous_episodes,
            entity_types=entity_types,
            excluded_entity_types=excluded_entity_types,
            custom_extraction_instructions=repair_instructions,
        )
        receipt.relationless_repair_succeeded = bool(edges)
        if not edges:
            # The repair prompt permits a truly relation-free turn to return both lists empty.
            # Do not let that second response erase the first response's evidence that content was
            # extracted and then lost: stamp_and_finalize must still take the visible failure path,
            # not misreport this as an ordinary zero-extraction success.
            receipt.raw_entity_count = max(
                receipt.raw_entity_count,
                receipt.relationless_initial_entity_count,
            )
            receipt.raw_edge_count = max(
                receipt.raw_edge_count,
                receipt.relationless_initial_edge_count,
            )
        logger.info(
            "Relationless combined extraction repair complete "
            "episode_id=%s succeeded=%s raw_entities=%d raw_edges=%d",
            receipt.episode_key,
            receipt.relationless_repair_succeeded,
            receipt.raw_entity_count,
            receipt.raw_edge_count,
        )
    if (
        endpoint is not None
        and receipt is not None
        and _unresolved_author_aliases(nodes, receipt)
    ):
        # Real models can privilege a familiar `user` convention even when a later instruction
        # declares a safer opaque endpoint. Do not reinterpret that string as provenance. Give the
        # model one bounded correction with no conflicting Menhir-authored `user` instruction;
        # final quarantine withholds unresolved references, even beside another valid marker.
        assert receipt is not None
        logger.warning(
            "Eligible extraction used an undeclared self-like endpoint; running one corrective "
            "retry episode_id=%s",
            receipt.episode_key,
        )
        correction_instructions = _combine_extraction_instructions(
            effective_instructions,
            _subject_endpoint_correction_instructions(endpoint),
            endpoint_instructions,
        )
        correction_previous_episodes = _relationless_repair_previous_episodes(
            episode,
            previous_episodes,
            receipt.relationless_repair_context_texts,
        )
        nodes, edges, index_map = await extract_nodes_and_edges(
            clients,
            episode,
            correction_previous_episodes,
            entity_types=entity_types,
            excluded_entity_types=excluded_entity_types,
            custom_extraction_instructions=correction_instructions,
        )
    # Bind the proven human AFTER the relationless-repair branch above: a repair re-runs
    # extraction and replaces nodes/edges/index_map wholesale, so binding before it would be
    # discarded. This is the last point where the payload is final and Graphiti has not yet
    # acquired candidates.
    if receipt is not None and receipt.self_identity is not None:
        receipt.self_bind_result = _record_self_binding(
            nodes, edges, index_map, receipt
        )

    if receipt is not None:
        receipt.resolved_node_count = len(nodes)
        receipt.resolved_edge_count = len(edges)
        surviving_inputs = (
            receipt.raw_entity_count
            - receipt.malformed_entities_dropped
            + receipt.endpoints_synthesized
        )
        receipt.orphan_nodes_dropped = max(0, surviving_inputs - len(nodes))
    return nodes, edges, index_map
