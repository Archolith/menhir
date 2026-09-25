"""Structural and View candidate isolation plus untyped attribute preservation patches."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _is_structural_graphiti_candidate(node: Any) -> bool:
    """Return whether a Graphiti candidate belongs to Menhir's structure graph.

    Graphiti materializes every non-core Neo4j property under ``EntityNode.attributes``.
    Structural nodes therefore remain distinguishable during candidate resolution even though
    they share the generic ``:Entity`` label with semantic memory nodes.
    """
    attributes = getattr(node, "attributes", None)
    return isinstance(attributes, dict) and attributes.get("structure_role") is not None


def _is_view_graphiti_candidate(node: Any) -> bool:
    """Return whether a Graphiti candidate is a Menhir View (scalar_state, counter, timeline...).

    Views are ``:Entity`` nodes with a name embedding, so Graphiti's semantic candidate search
    returns them like any memory node, and its dedupe then resolves an extracted entity ONTO
    the View. Issue #94: "Alice owns 37 coins." produced a counter View named
    "alice's coins: 37 ...", the next extraction resolved its "coins" entity onto that View,
    Graphiti wrote `(:Episodic)-[:MENTIONS]->(view)` and `(Alice)-[:OWNS]->(view)`, and the
    View's provenance gate -- correctly -- refused every refresh from then on.

    A View is derived state and must never be an identity-resolution target. Checked on the
    same materialized attributes the structural predicate uses; `is_view` is what the View
    writer stamps (`view_write_repository.py`), `view_kind`/`view_class` are its siblings.
    """
    attributes = getattr(node, "attributes", None)
    if not isinstance(attributes, dict):
        return False
    return (
        bool(attributes.get("is_view"))
        or attributes.get("view_kind") is not None
        or attributes.get("view_class") is not None
    )


def _patch_graphiti_structural_candidate_isolation() -> None:
    """Prevent semantic enrichment from resolving onto structural ``:Entity`` nodes.

    Graphiti's semantic candidate search has no knowledge of Menhir's structural/semantic
    boundary.  An extracted project or file name can therefore resolve to an existing structure
    node and send that node through Graphiti's hydration + replacement-save path.  Filter after
    candidate collection so both search results and ``existing_nodes_override`` inputs obey the
    boundary, while semantic candidates retain their original order.
    """
    try:
        import graphiti_core.utils.maintenance.node_operations as _no_module

        if getattr(_no_module, "_menhir_structural_candidate_isolation_patched", False):
            return

        _original_collect_candidate_nodes = _no_module._collect_candidate_nodes

        async def _collect_non_structural_candidates(
            clients,
            extracted_nodes,
            existing_nodes_override,
        ):
            candidates_by_extracted = await _original_collect_candidate_nodes(
                clients, extracted_nodes, existing_nodes_override
            )
            dropped = 0
            filtered: list[list[Any]] = []
            for candidates in candidates_by_extracted:
                eligible = [
                    candidate
                    for candidate in candidates
                    if not _is_structural_graphiti_candidate(candidate)
                    and not _is_view_graphiti_candidate(candidate)
                ]
                dropped += len(candidates) - len(eligible)
                filtered.append(eligible)
            if dropped:
                logger.info(
                    "Excluded %d structural/View node candidate(s) from Graphiti semantic dedup",
                    dropped,
                )
            return filtered

        _no_module._collect_candidate_nodes = (  # type: ignore[assignment]
            _collect_non_structural_candidates
        )
        _no_module._menhir_structural_candidate_isolation_patched = True  # type: ignore[attr-defined]
        logger.debug("Graphiti structural candidate isolation patch applied")
    except (ImportError, AttributeError) as exc:
        logger.warning("Failed to patch Graphiti structural candidate isolation: %s", exc)


def _patch_graphiti_untyped_attribute_preservation() -> None:
    """Preserve existing properties when Graphiti has no typed attribute schema.

    Graphiti 0.29.2 returns ``{}`` for an untyped node, assigns that result to
    ``EntityNode.attributes``, then persists the node with ``SET n = node``.  Any properties owned
    outside Graphiti are erased.  Keeping a copy of the existing attributes closes that generic
    replacement-save hazard even for non-structural nodes and provides defense in depth behind the
    structural candidate filter.
    """
    try:
        import graphiti_core.utils.maintenance.node_operations as _no_module

        if getattr(_no_module, "_menhir_untyped_attribute_preservation_patched", False):
            return

        _original_extract_entity_attributes = _no_module._extract_entity_attributes

        async def _extract_entity_attributes_preserving_existing(
            llm_client,
            node,
            episode,
            previous_episodes,
            entity_type,
        ):
            model_fields = getattr(entity_type, "model_fields", None)
            if entity_type is None or not model_fields:
                return dict(getattr(node, "attributes", None) or {})
            return await _original_extract_entity_attributes(
                llm_client,
                node,
                episode,
                previous_episodes,
                entity_type,
            )

        _no_module._extract_entity_attributes = (  # type: ignore[assignment]
            _extract_entity_attributes_preserving_existing
        )
        _no_module._menhir_untyped_attribute_preservation_patched = True  # type: ignore[attr-defined]
        logger.debug("Graphiti untyped attribute preservation patch applied")
    except (ImportError, AttributeError) as exc:
        logger.warning("Failed to patch Graphiti untyped attribute preservation: %s", exc)
