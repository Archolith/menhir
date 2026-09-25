"""Consolidation, decay, session management, and conflict resolution queries.

Extracted from MemoryGraphAdapter. The adapter delegates all Entity decay,
session promotion, and conflict governance operations here.

The implementation lives in ``consolidation_queries_*`` sibling modules, composed
into ``ConsolidationRepository`` below; every moved public symbol is re-exported
here so the original import path keeps working unchanged.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from menhir.infrastructure.consolidation_queries_common import (
    _DECAY_CANDIDATE_LIMIT,
    _content_overlap_ratio,
    automatic_lifecycle_protection_cypher,
    harmful_automatic_mutation_allowed_cypher,
)
from menhir.infrastructure.consolidation_queries_conflicts import (
    ConsolidationConflictGovernanceMixin,
)
from menhir.infrastructure.consolidation_queries_decay import (
    ConsolidationDecayCompressionMixin,
)
from menhir.infrastructure.consolidation_queries_edges import (
    ConsolidationGraphMaintenanceMixin,
)
from menhir.infrastructure.consolidation_queries_resolution import (
    ConsolidationConflictResolutionMixin,
)
from menhir.infrastructure.consolidation_queries_sessions import (
    ConsolidationSessionScopeMixin,
)
from menhir.infrastructure.neo4j import Neo4jRepository

logger = logging.getLogger(__name__)


@dataclass
class ConsolidationRepository(
    ConsolidationGraphMaintenanceMixin,
    ConsolidationDecayCompressionMixin,
    ConsolidationSessionScopeMixin,
    ConsolidationConflictGovernanceMixin,
    ConsolidationConflictResolutionMixin,
):
    """Encapsulates Entity decay, session promotion, and conflict resolution."""

    neo4j: Neo4jRepository

    def set_conflict(
        self,
        node_uuid_a: str,
        node_uuid_b: str,
        new_group_id: str,
        *,
        initial_status: str = "pending_llm_review",
    ) -> tuple[str, int]:
        """Mark two nodes as conflicting, joining or merging any existing groups.

        The caller passes ``new_group_id`` as a fallback; if either node already
        belongs to a conflict group the existing ID is used as canonical.  When
        both nodes are in *different* existing groups all members of the retired
        group are migrated into the canonical one.

        ``initial_status`` defaults to ``'pending_llm_review'`` so new detections
        wait for LLM confirmation before surfacing to the user as ``'unresolved'``.

        Returns (canonical_group_id, nodes_updated).
        """
        rows = self.neo4j.execute(
            f"""
            MATCH (a:Entity), (b:Entity)
            WHERE a.uuid = $uuid_a AND b.uuid = $uuid_b
              AND {automatic_lifecycle_protection_cypher("a")}
              AND {automatic_lifecycle_protection_cypher("b")}
            WITH a, b,
              CASE
                WHEN a.conflict_group_id IS NOT NULL THEN a.conflict_group_id
                WHEN b.conflict_group_id IS NOT NULL THEN b.conflict_group_id
                ELSE $new_group_id
              END AS canonical_group_id,
              CASE
                WHEN a.conflict_group_id IS NOT NULL
                     AND b.conflict_group_id IS NOT NULL
                     AND a.conflict_group_id <> b.conflict_group_id
                THEN b.conflict_group_id
                ELSE null
              END AS retired_group_id
            OPTIONAL MATCH (orphan:Entity)
            WHERE orphan.conflict_group_id = retired_group_id
              AND {automatic_lifecycle_protection_cypher("orphan")}
            WITH a, b, canonical_group_id, collect(orphan) AS orphans
            SET a.conflict_group_id  = canonical_group_id,
                a.conflict_status    = $initial_status,
                a.conflict_created_at = coalesce(a.conflict_created_at, datetime()),
                b.conflict_group_id  = canonical_group_id,
                b.conflict_status    = $initial_status,
                b.conflict_created_at = coalesce(b.conflict_created_at, datetime())
            FOREACH (o IN orphans |
                SET o.conflict_group_id = canonical_group_id,
                    o.conflict_status   = $initial_status
            )
            RETURN canonical_group_id, 2 + size(orphans) AS updated
            """,
            params={
                "uuid_a": node_uuid_a,
                "uuid_b": node_uuid_b,
                "new_group_id": new_group_id,
                "initial_status": initial_status,
            },
        )
        if not rows:
            return new_group_id, 0
        canonical = str(rows[0].get("canonical_group_id") or new_group_id)
        updated = int(rows[0].get("updated", 0))
        return canonical, updated
