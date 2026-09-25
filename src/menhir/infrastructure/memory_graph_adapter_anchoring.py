"""Structural anchoring delegates for the memory graph adapter.

Methods moved verbatim from ``memory_graph_adapter.py`` (facade split); this
mixin is composed into :class:`~menhir.infrastructure.memory_graph_adapter.MemoryGraphAdapter`.
"""

from __future__ import annotations


class MemoryGraphAnchoringMixin:
    """Structural anchoring delegates (mixin for MemoryGraphAdapter)."""

    # -------------------------------------------------------------------------
    # Structural anchoring delegates
    # -------------------------------------------------------------------------

    def anchor_semantic_to_structural(
        self,
        semantic_uuids: list[str],
        candidate_paths: list[str],
        *,
        anchor_source: str = "narrative_path",
        weight: float = 1.0,
        project_filter: str | None = None,
    ) -> int:
        """Resolve file paths and create ANCHORED_TO edges from semantic entities."""
        from menhir.infrastructure.structural_anchoring import (
            resolve_structural_entities,
            create_anchor_edges,
        )

        resolved = resolve_structural_entities(
            self.neo4j, candidate_paths, project_filter
        )
        if not resolved:
            return 0
        structural_uuids = [r["uuid"] for r in resolved]
        return create_anchor_edges(
            self.neo4j,
            semantic_uuids,
            structural_uuids,
            anchor_source=anchor_source,
            weight=weight,
        )

    def find_cross_linked_semantic_entities(
        self, structural_uuids: list[str]
    ) -> list[str]:
        """Find semantic entity UUIDs anchored to the given structural entities."""
        from menhir.infrastructure.structural_anchoring import (
            find_cross_linked_semantic_entities,
        )

        return find_cross_linked_semantic_entities(self.neo4j, structural_uuids)
