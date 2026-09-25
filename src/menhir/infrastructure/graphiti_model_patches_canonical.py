"""Canonical-self resolution helpers and the adaptive node-dedupe batching patch."""

from __future__ import annotations

import logging
from typing import Any

from menhir.infrastructure.graphiti_llm_patches import GraphitiRequestTooLargeError

logger = logging.getLogger(__name__)


def _stamp_canonical_self(node: Any, identity: Any) -> Any:
    """Put the canonical markers on the node that will be persisted.

    Graphiti writes `attributes` into the node's property map, and the generic ingest metadata
    stamp supplies neither marker. Without this the FIRST canonical node in a namespace is created
    without `is_self`/`entity_role`, so every reader that identifies the human structurally --
    fork detection, census, migration disposition -- would not recognize the node this change
    just created.
    """
    try:
        attributes = getattr(node, "attributes", None)
        if attributes is None:
            return node
        attributes["is_self"] = True
        attributes["entity_role"] = "self"
        if identity is not None and getattr(identity, "namespace", ""):
            attributes["namespace"] = identity.namespace
    except Exception:  # noqa: BLE001 - never fail resolution on a metadata stamp
        logger.exception("Could not stamp canonical-self markers")
    return node


async def _existing_canonical_node(clients: Any, extracted: Any, identity: Any) -> Any:
    """Return the persisted canonical self node, or a stamped *extracted* when none exists yet.

    First trusted-self episode in a namespace: nothing is stored, so the extracted node IS the
    canonical node and creating it is correct -- but it must carry the canonical markers, which
    is why this is the authoritative persistence boundary for them.

    Every episode after that: the stored node carries state the extraction does not have, and must
    be the object graphiti writes back.

    **Only a genuinely absent node falls back to the extracted object.** Graphiti persists a
    resolved node with `SET n = $entity_data`, which REPLACES the property map, so treating a
    transient driver or database failure as "absent" would let a later successful write erase the
    canonical node's markers, provenance, flags and accumulated summary. An operational failure
    must fail the episode, which is retryable, rather than silently degrade to a sparse overwrite.
    """
    from graphiti_core.errors import NodeNotFoundError
    from graphiti_core.nodes import EntityNode

    driver = getattr(clients, "driver", None)
    if driver is None:
        # An absent driver is an operational invariant failure, not evidence that the canonical
        # node does not exist. Falling back here would commit the sparse extracted node and let
        # graphiti's replacing save erase the stored one -- the same defect as swallowing a
        # transient read error, reached by a different door.
        raise RuntimeError(
            "canonical-self resolution requires a graph driver; refusing to substitute the "
            "extracted node for an unread canonical node"
        )
    try:
        stored = await EntityNode.get_by_uuid(driver, extracted.uuid)
    except NodeNotFoundError:
        return _stamp_canonical_self(extracted, identity)
    if identity is not None:
        from menhir.domain.namespace import namespace_to_group_id

        expected_group = namespace_to_group_id(identity.namespace)
        actual_group = getattr(stored, "group_id", None)
        if actual_group is None or str(actual_group) != expected_group:
            raise RuntimeError(
                f"stored canonical-self node {extracted.uuid!r} belongs to physical group "
                f"{actual_group!r}, expected {expected_group!r} for logical namespace "
                f"{identity.namespace!r}; refusing cross-namespace resolution"
            )
    return stored


def _active_self_identity() -> Any:
    """The identity context for the current episode, if binding ran."""
    try:
        from menhir.infrastructure.graphiti_extraction_patches import get_extraction_receipt

        receipt = get_extraction_receipt()
    except Exception:  # noqa: BLE001
        return None
    return getattr(receipt, "self_identity", None) if receipt is not None else None


def _pre_resolved_self_uuid() -> str | None:
    """The canonical self uuid bound for the current episode, if binding ran and succeeded.

    Read from the task-local extraction receipt rather than passed down, because Graphiti owns
    the call signature between extraction and resolution.
    """
    try:
        from menhir.infrastructure.graphiti_extraction_patches import get_extraction_receipt

        receipt = get_extraction_receipt()
    except Exception:  # noqa: BLE001 - resolution must never fail on instrumentation
        return None
    result = getattr(receipt, "self_bind_result", None) if receipt is not None else None
    if result is None or not getattr(result, "bound", False):
        return None
    return getattr(result, "self_uuid", None)


def _canonical_self_candidate_filter_enabled() -> bool:
    """Candidate isolation mutates resolution, so it belongs to ENFORCE only.

    OFF must reproduce the old resolver and OBSERVE must measure without changing ingest. The
    receipt stores a StrEnum, but compare its string form so this helper stays decoupled from the
    binding module and fails closed when no receipt exists.
    """
    try:
        from menhir.infrastructure.graphiti_extraction_patches import get_extraction_receipt

        receipt = get_extraction_receipt()
    except Exception:  # noqa: BLE001
        return False
    return bool(
        receipt is not None
        and str(getattr(receipt, "self_bind_mode", "") or "") == "enforce"
    )


def _is_canonical_self_candidate(node: Any, identity: Any) -> bool:
    """Protect canonical self from every ordinary Graphiti resolution path.

    A declaration-bound node is removed from candidate search entirely. Every node that remains
    searchable is therefore unproven and must not reach canonical self through exact-name,
    similarity, an LLM choice, or ``existing_nodes_override``. Markers cover canonical nodes from
    any namespace; the deterministic UUID covers an incompletely stamped node in this namespace.
    """
    attributes = getattr(node, "attributes", None)
    if isinstance(attributes, dict) and (
        attributes.get("is_self") is True
        or str(attributes.get("entity_role") or "").strip().casefold() == "self"
    ):
        return True
    expected_uuid = str(getattr(identity, "self_uuid", "") or "")
    return bool(expected_uuid) and str(getattr(node, "uuid", "") or "") == expected_uuid


def _patch_graphiti_adaptive_dedupe() -> None:
    """Split oversized node-deduplication requests without reducing candidate quality.

    Graphiti 0.29 retrieves up to 15 candidates for every extracted entity, merges all
    candidates for all unresolved entities, and sends one LLM request.  Large but valid
    episodes can therefore fan out into a request hundreds of times larger than the
    episode itself.  Keep Graphiti's normal one-request path, but when the assembled
    request exceeds the local/provider context limit, bisect the unresolved entities
    and retry each half with only the candidates retrieved for that half.

    Candidate search and deterministic similarity resolution still run exactly once.
    The fallback changes neither the per-entity candidate limit nor candidate order.
    """

    try:
        import graphiti_core.graphiti as _graphiti_module
        import graphiti_core.utils.bulk_utils as _bulk_module
        import graphiti_core.utils.maintenance.node_operations as _no_module
        from graphiti_core.utils.maintenance.dedup_helpers import DedupResolutionState

        if getattr(_no_module, "_menhir_adaptive_dedupe_patched", False):
            return

        async def _adaptive_resolve_extracted_nodes(
            clients,
            extracted_nodes,
            episode=None,
            previous_episodes=None,
            entity_types=None,
            existing_nodes_override=None,
        ):
            # The Menhir hook helpers resolve through the facade module's namespace at call
            # time, preserving the single-module late binding the original
            # graphiti_model_patches namespace exposed to tests (e.g. patching
            # graphiti_model_patches._existing_canonical_node must reach this resolver).
            from menhir.infrastructure import graphiti_model_patches as _facade

            llm_client = clients.llm_client

            # A node already bound to the deterministic canonical-self uuid is authoritative by
            # construction: trusted episode metadata proved the author, so there is nothing for
            # similarity or an LLM to decide. It is withheld from _collect_candidate_nodes
            # entirely -- not merely skipped afterwards -- because the cosine search IS the
            # mechanism that fragmented this identity: the `user` candidate window saturates
            # with exact-name matches, making the deterministic single-match branch unreachable
            # and routing every extraction to the LLM.
            prompt_sections: list[dict[str, int]] = []
            pre_resolved_indices: set[int] = set()
            _bound_uuid = _facade._pre_resolved_self_uuid()
            if _bound_uuid:
                pre_resolved_indices = {
                    idx
                    for idx, node in enumerate(extracted_nodes)
                    if str(getattr(node, "uuid", "") or "") == _bound_uuid
                }
                logger.info(
                    "Canonical-self resolver pre-resolved uuid=%s matches=%d",
                    _bound_uuid,
                    len(pre_resolved_indices),
                )

            searchable = [n for i, n in enumerate(extracted_nodes) if i not in pre_resolved_indices]
            candidate_filter_enabled = _facade._canonical_self_candidate_filter_enabled()
            identity = _facade._active_self_identity() if candidate_filter_enabled else None
            if candidate_filter_enabled:
                undeclared_canonical_nodes = [
                    node
                    for node in searchable
                    if _facade._is_canonical_self_candidate(node, identity)
                ]
                if undeclared_canonical_nodes:
                    raise RuntimeError(
                        "undeclared extracted node carries canonical-self identity in enforce "
                        "mode; refusing ordinary Graphiti resolution"
                    )
            searched = await _no_module._collect_candidate_nodes(
                clients,
                searchable,
                existing_nodes_override,
            )
            # The declaration-bound node never enters search. Conversely, an ordinary searchable
            # node must never acquire the canonical UUID through Graphiti's name/similarity/LLM
            # path. Endpoint closure can retain an ordinary node named `user`; without this filter
            # a unique exact match would silently turn that name back into identity authority.
            if candidate_filter_enabled:
                canonical_candidates_excluded = 0
                protected_search_results: list[list[Any]] = []
                for candidates in searched:
                    eligible = [
                        candidate
                        for candidate in candidates
                        if not _facade._is_canonical_self_candidate(candidate, identity)
                    ]
                    canonical_candidates_excluded += len(candidates) - len(eligible)
                    protected_search_results.append(eligible)
                searched = protected_search_results
                if canonical_candidates_excluded:
                    logger.info(
                        "Excluded %d canonical-self candidate(s) from undeclared Graphiti dedup",
                        canonical_candidates_excluded,
                    )
            # Realign to the full extracted list; pre-resolved nodes get no candidates.
            _searched_iter = iter(searched)
            candidate_nodes_by_extracted = [
                [] if i in pre_resolved_indices else next(_searched_iter)
                for i in range(len(extracted_nodes))
            ]

            state = DedupResolutionState(
                resolved_nodes=[None] * len(extracted_nodes),
                uuid_map={},
                unresolved_indices=[],
            )

            for idx in pre_resolved_indices:
                node = extracted_nodes[idx]
                # Commit the EXISTING canonical node when there is one, not the freshly extracted
                # object. Graphiti persists a resolved node with `SET n = $entity_data`, which
                # REPLACES the property map rather than merging it, so committing the extraction
                # would wipe the canonical node's `is_self`, `entity_role`, `namespace`,
                # `user_flagged`, provenance and accumulated summary on every subsequent self
                # episode. The ordinary path avoids this precisely because `_promote_resolved_node`
                # returns the hydrated database node; the bypass has to do the same.
                #
                # This is a direct uuid fetch, not candidate acquisition: no cosine search, no
                # exact/fuzzy resolution, no dedup LLM, no identity gate. D4 is preserved.
                state.resolved_nodes[idx] = await _facade._existing_canonical_node(
                    clients, node, _facade._active_self_identity()
                )
                state.uuid_map[node.uuid] = node.uuid

            for idx, (node, candidates) in enumerate(
                zip(extracted_nodes, candidate_nodes_by_extracted, strict=True)
            ):
                if idx in pre_resolved_indices or not candidates:
                    continue

                indexes = _no_module._build_candidate_indexes(candidates)
                local_state = DedupResolutionState(
                    resolved_nodes=[None], uuid_map={}, unresolved_indices=[]
                )
                _no_module._resolve_with_similarity([node], indexes, local_state)
                if local_state.resolved_nodes[0] is not None:
                    _no_module._commit_resolution(
                        state,
                        local_state.resolved_nodes[0],
                        local_state.uuid_map,
                        local_state.duplicate_pairs,
                        idx,
                    )
                    continue

                state.unresolved_indices.append(idx)

            async def _resolve_batch(indices: list[int], depth: int = 0) -> None:
                candidate_nodes = _no_module._merge_candidate_nodes(
                    [
                        candidate
                        for idx in indices
                        for candidate in candidate_nodes_by_extracted[idx]
                    ],
                    None,
                )
                batch_state = DedupResolutionState(
                    resolved_nodes=[None] * len(extracted_nodes),
                    uuid_map={},
                    unresolved_indices=list(indices),
                )
                try:
                    prompt_sections.append(
                        _facade._measure_prompt_sections(
                            [extracted_nodes[i] for i in indices],
                            candidate_nodes,
                            entity_types,
                            episode,
                            previous_episodes,
                        )
                    )
                except Exception:  # noqa: BLE001 - instrumentation only
                    logger.debug("Prompt-section measurement failed", exc_info=True)

                try:
                    await _no_module._resolve_with_llm(
                        llm_client,
                        extracted_nodes,
                        _no_module._build_candidate_indexes(candidate_nodes),
                        batch_state,
                        episode,
                        previous_episodes,
                        entity_types,
                    )
                except GraphitiRequestTooLargeError:
                    if len(indices) <= 1:
                        logger.error(
                            "Graphiti node-dedupe request remains oversized for one entity "
                            "candidate_count=%d split_depth=%d",
                            len(candidate_nodes),
                            depth,
                        )
                        raise

                    midpoint = len(indices) // 2
                    left = indices[:midpoint]
                    right = indices[midpoint:]
                    logger.warning(
                        "Graphiti node-dedupe request oversized; splitting entity batch "
                        "entities=%d candidates=%d split_depth=%d left=%d right=%d",
                        len(indices),
                        len(candidate_nodes),
                        depth,
                        len(left),
                        len(right),
                    )
                    await _resolve_batch(left, depth + 1)
                    await _resolve_batch(right, depth + 1)
                    return

                for idx in indices:
                    resolved_node = batch_state.resolved_nodes[idx]
                    if resolved_node is not None:
                        state.resolved_nodes[idx] = resolved_node
                state.uuid_map.update(batch_state.uuid_map)
                state.duplicate_pairs.extend(batch_state.duplicate_pairs)

            escalated = list(state.unresolved_indices)
            if state.unresolved_indices:
                await _resolve_batch(list(state.unresolved_indices))
            _facade._record_resolution_outcomes(
                clients, extracted_nodes, candidate_nodes_by_extracted, escalated,
                state, pre_resolved_indices, prompt_sections,
            )

            if not state.unresolved_indices and not any(candidate_nodes_by_extracted):
                logger.debug("No semantic dedup candidates found; keeping all extracted nodes as new")

            for idx, node in enumerate(extracted_nodes):
                if state.resolved_nodes[idx] is None:
                    state.resolved_nodes[idx] = node
                    state.uuid_map[node.uuid] = node.uuid

            logger.debug(
                "Resolved nodes with adaptive dedupe: %s",
                [node.uuid for node in state.resolved_nodes if node is not None],
            )
            return (
                [node for node in state.resolved_nodes if node is not None],
                state.uuid_map,
                state.duplicate_pairs,
            )

        _no_module.resolve_extracted_nodes = _adaptive_resolve_extracted_nodes
        _graphiti_module.resolve_extracted_nodes = _adaptive_resolve_extracted_nodes
        _bulk_module.resolve_extracted_nodes = _adaptive_resolve_extracted_nodes
        _no_module._menhir_adaptive_dedupe_patched = True
        logger.debug("Graphiti adaptive node-dedupe batching patch applied")
    except (ImportError, AttributeError) as exc:
        logger.warning("Failed to patch Graphiti adaptive node dedupe: %s", exc)
