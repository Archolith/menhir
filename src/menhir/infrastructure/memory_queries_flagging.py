"""Flag, unflag, and promote mutations for the memory read repository.

Split from ``memory_queries.py``; method bodies are verbatim, composed into
``MemoryQueryRepository`` by the facade module.
"""

from __future__ import annotations

from typing import Any

from menhir.domain.bootstrap_scope import normalize_bootstrap_scope
from menhir.domain.structural_memory import non_structural_memory_cypher


class MemoryQueryFlaggingMixin:
    """Flag/unflag/promote methods of ``MemoryQueryRepository``."""

    # --- Flag & delete ------------------------------------------------------

    def flag_memory(
        self, node_uuid: str, bootstrap_scope: str | None = None
    ) -> bool:
        """Persist the explicit v1 retention override on a node.

        Rejects structural graph nodes (project-scan entities) because they
        are not semantic memories and waste bootstrap tokens.
        """

        # Check if the target is a structural entity before flagging.
        struct_check = self.neo4j.execute(
            f"""
            MATCH (n:Entity {{uuid: $node_uuid}})
            WHERE NOT ({non_structural_memory_cypher("n")})
            RETURN coalesce(n.structure_role, 'legacy_project_scan') AS role LIMIT 1
            """,
            params={"node_uuid": node_uuid},
        )
        if struct_check:
            role = struct_check[0].get("role", "unknown")
            raise ValueError(
                f"Cannot flag structural graph node (role={role}). "
                "Flag semantic memories only."
            )

        normalized_scope = (
            normalize_bootstrap_scope(bootstrap_scope)
            if bootstrap_scope is not None
            else None
        )
        scope_set = (
            "n.bootstrap_scope = $bootstrap_scope"
            if bootstrap_scope is not None
            else "n.bootstrap_scope = n.bootstrap_scope"
        )
        params: dict[str, Any] = {"node_uuid": node_uuid}
        if bootstrap_scope is not None:
            params["bootstrap_scope"] = normalized_scope
        rows = self.neo4j.execute(
            f"""
            MATCH (n)
            WHERE (n:Entity OR n:Episodic) AND n.uuid = $node_uuid
            SET n.user_flagged = true,
                {scope_set}
            RETURN count(n) AS nodes_updated
            """,
            params=params,
        )
        return bool(rows and int(rows[0].get("nodes_updated", 0)) > 0)

    def unflag_memory(self, node_uuid: str) -> bool:
        """Remove the explicit user retention override from a memory node.

        Unlike flag_memory, this does NOT check for structural nodes —
        removing a flag from any node type is safe and idempotent.
        """

        rows = self.neo4j.execute(
            """
            MATCH (n)
            WHERE (n:Entity OR n:Episodic) AND n.uuid = $node_uuid
            SET n.user_flagged = false,
                n.bootstrap_scope = null
            RETURN count(n) AS nodes_updated
            """,
            params={"node_uuid": node_uuid},
        )
        return bool(rows and int(rows[0].get("nodes_updated", 0)) > 0)

    def promote_memory(self, node_uuid: str) -> bool:
        """Promote a PERSISTENT memory to PROMOTED: operator-curated, verified ground truth (SSOT-08).

        Distinct from user_flagged (importance to the user, auto-propagated,
        decay-immune but still an ordinary claim): PROMOTED is a stronger,
        deliberate curation action, never auto-set, meaning "this claim is
        verified and cannot be false." Confidence is pinned at 1.0 at
        promotion time; CorrelationService.classify_pair separately refuses
        to ever merge a PROMOTED node into/out of another identity, so this
        pin cannot drift via absorption afterward.

        Guarded on scope='PERSISTENT' — only an already-durable memory can be
        promoted (never SESSION/CANDIDATE, which haven't earned durability yet).
        Idempotent: promoting an already-PROMOTED node is a safe no-op success.

        Returns True if a PERSISTENT (or already-PROMOTED) node was updated,
        False if no such node exists.
        """
        rows = self.neo4j.execute(
            """
            MATCH (n:Entity)
            WHERE n.uuid = $node_uuid AND n.scope IN ['PERSISTENT', 'PROMOTED']
            SET n.scope = 'PROMOTED',
                n.source_confidence = 1.0,
                n.promoted_at = coalesce(n.promoted_at, datetime())
            RETURN count(n) AS nodes_updated
            """,
            params={"node_uuid": node_uuid},
        )
        return bool(rows and int(rows[0].get("nodes_updated", 0)) > 0)
