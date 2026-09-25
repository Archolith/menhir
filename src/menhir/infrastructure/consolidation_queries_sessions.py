"""SESSION-scope lifecycle queries: fetch, promote, demote, and delete.

Moved verbatim from ``consolidation_queries.py`` and composed into
``ConsolidationRepository`` via this mixin.
"""

from __future__ import annotations

from typing import Any

from menhir.infrastructure.consolidation_queries_common import (
    automatic_lifecycle_protection_cypher,
    harmful_automatic_mutation_allowed_cypher,
)
from menhir.infrastructure.cypher import Cypher
from menhir.infrastructure.neo4j import SAGA_MUTATION_TIMEOUT_S


class ConsolidationSessionScopeMixin:
    """Session and scope management half of ``ConsolidationRepository``."""

    # -------------------------------------------------------------------------
    # Session / scope management
    # -------------------------------------------------------------------------

    def fetch_session_entities(
        self,
        session_id: str | None = None,
        max_age_hours: float = 0,
    ) -> list[dict[str, Any]]:
        """Fetch SESSION-scoped Entity nodes eligible for consolidation.

        Includes ttl_expires for observability; the expiry decision is DB-side only.
        """

        params: dict[str, Any] = {}
        if session_id is not None:
            params["session_id"] = session_id
        if max_age_hours > 0:
            params["max_age_hours"] = max_age_hours

        query = (Cypher()
            .match("(n:Entity)")
            .where("n.scope = 'SESSION'", automatic_lifecycle_protection_cypher("n"))
            .where_if(session_id is not None, "n.session_id = $session_id")
            .where_if(max_age_hours > 0, "n.created_at < datetime() - duration({hours: $max_age_hours})")
            .return_raw("""n.uuid AS uuid,
       n.name AS name,
       n.content AS content,
       n.summary AS summary,
       n.type AS type,
       n.sharpness AS sharpness,
       n.user_flagged AS user_flagged,
       n.session_id AS session_id,
       coalesce(n.namespace, 'default') AS namespace,
       n.created_at AS created_at,
       n.ttl_expires AS ttl_expires""")
            .order_by("n.created_at ASC")
            .build())
        return self.neo4j.execute(query, params=params)

    def count_persistent_edges(self, node_uuid: str) -> int:
        """Count edges from a node to PERSISTENT or PROMOTED semantic nodes.

        Excludes ANCHORED_TO edges and structural neighbors (structure_role
        IS NOT NULL) so cross-domain anchor links don't inflate the promotion
        threshold during session consolidation.
        """

        rows = self.neo4j.execute(
            """
            MATCH (n:Entity)-[r]-(other:Entity)
            WHERE n.uuid = $node_uuid
              AND other.scope IN ['PERSISTENT', 'PROMOTED']
              AND NOT type(r) = 'ANCHORED_TO'
              AND other.structure_role IS NULL
            RETURN count(DISTINCT other) AS persistent_neighbors
            """,
            params={"node_uuid": node_uuid},
        )
        return int(rows[0].get("persistent_neighbors", 0)) if rows else 0

    def promote_to_persistent(self, node_uuids: list[str]) -> int:
        """Promote SESSION nodes to PERSISTENT with ACTIVE freshness.

        Clears any pending demotion (ttl_expires) — a rescued node is no longer demoted.
        """

        if not node_uuids:
            return 0
        query = (Cypher()
            .match("(n:Entity)")
            .where("n.uuid IN $uuids", "n.scope = 'SESSION'",
                   automatic_lifecycle_protection_cypher("n"))
            .set(("n.scope = 'PERSISTENT'",
                  "n.freshness = 'ACTIVE'",
                  "n.promoted_at = datetime()",
                  "n.ttl_expires = null"))
            .return_raw("count(n) AS promoted")
            .build())
        rows = self.neo4j.execute(query, params={"uuids": node_uuids})
        return int(rows[0].get("promoted", 0)) if rows else 0

    def delete_session_nodes(self, node_uuids: list[str]) -> int:
        """DETACH DELETE the given SESSION Entity nodes."""

        if not node_uuids:
            return 0
        query = (Cypher()
            .match("(n:Entity)")
            .where("n.uuid IN $uuids", "n.scope = 'SESSION'",
                   harmful_automatic_mutation_allowed_cypher("n"))
            .detach_delete("n")
            .return_raw("count(n) AS deleted")
            .build())
        rows = self.neo4j.execute(query, params={"uuids": node_uuids})
        return int(rows[0].get("deleted", 0)) if rows else 0

    def delete_entities_returning_uuids(
        self,
        node_uuids: list[str],
        *,
        require_scope: str | None = None,
        protect_retention: bool = False,
    ) -> list[str]:
        """DETACH DELETE the given Entity nodes and return the uuids ACTUALLY deleted (plan Phase 6).

        The existing `delete_session_nodes` returns only a COUNT, so a caller could never tell WHICH
        of its targets died -- and because the scope filter silently skips a node that was promoted in
        the race window, the pre-logged audit could claim a deletion that never happened. Returning
        the exact uuids from the mutation itself is what lets the coordinator audit the truth instead
        of its intent.
        """
        if not node_uuids:
            return []
        scope_clause = "AND n.scope = $scope" if require_scope else ""
        params: dict[str, Any] = {"uuids": node_uuids}
        if require_scope:
            params["scope"] = require_scope
        protection = (
            harmful_automatic_mutation_allowed_cypher("n")
            if protect_retention
            else automatic_lifecycle_protection_cypher("n")
        )
        rows = self.neo4j.execute(
            f"""
            MATCH (n:Entity)
            WHERE n.uuid IN $uuids {scope_clause}
              AND {protection}
            WITH collect(n) AS doomed, collect(n.uuid) AS deleted_uuids
            FOREACH (d IN doomed | DETACH DELETE d)
            RETURN deleted_uuids
            """,
            params=params,
            timeout_s=SAGA_MUTATION_TIMEOUT_S,  # bounded for ownership ageing (CF-211)
        )
        return [str(u) for u in (rows[0].get("deleted_uuids") or [])] if rows else []

    def newly_unreferenced_evidence(self, node_uuids: list[str]) -> list[str]:
        """Evidence that would be left referenced by NOTHING once these nodes are deleted.

        REPORT ONLY -- this never deletes (plan section 8). Evidence ownership and sharing semantics
        are a separate design decision, and cascade-deleting it merely because it fell to degree zero
        is precisely the inference that destroyed ~24 nodes on 2026-07-12. Isolation is not
        authorization.
        """
        if not node_uuids:
            return []
        rows = self.neo4j.execute(
            """
            MATCH (e:Evidence)<-[:SUPPORTED_BY]-(n:Entity)
            WHERE n.uuid IN $uuids
            WITH e, collect(DISTINCT n.uuid) AS doomed_refs
            MATCH (e)<-[:SUPPORTED_BY]-(other:Entity)
            WITH e, doomed_refs, collect(DISTINCT other.uuid) AS all_refs
            WHERE size([x IN all_refs WHERE NOT x IN doomed_refs]) = 0
            RETURN collect(DISTINCT e.uuid) AS orphaned
            """,
            params={"uuids": node_uuids},
        )
        return [str(u) for u in (rows[0].get("orphaned") or [])] if rows else []

    def set_demote_ttl(self, node_uuids: list[str], ttl_days: int) -> int:
        """Set a time-to-live for SESSION nodes (F5 demotion grace window).

        Uses coalesce to set once: if a node already has ttl_expires, it is NOT reset.
        Only nodes without a prior TTL are newly demoted.

        Returns the count of nodes that had NO prior TTL (newly demoted).
        """

        if not node_uuids:
            return 0

        # Decide and mutate in one statement so a source flag added after candidate discovery
        # cannot race the TTL write.
        query = (Cypher()
            .match("(n:Entity)")
            .where("n.uuid IN $uuids", "n.scope = 'SESSION'", "n.ttl_expires IS NULL",
                   harmful_automatic_mutation_allowed_cypher("n"))
            .set("n.ttl_expires = datetime() + duration({days: $days})")
            .return_raw("count(n) AS newly_demoted_count")
            .build())
        rows = self.neo4j.execute(query, params={"uuids": node_uuids, "days": ttl_days})
        return int(rows[0].get("newly_demoted_count", 0)) if rows else 0

    def fetch_ttl_expired_session_uuids(
        self,
        session_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch SESSION nodes whose ttl_expires has passed (expired demoted nodes to delete).

        Returns uuid, name, session_id for each expired node (for audit records before deletion).
        """

        params: dict[str, Any] = {}
        if session_id is not None:
            params["session_id"] = session_id

        query = (Cypher()
            .match("(n:Entity)")
            .where("n.scope = 'SESSION'",
                   "n.ttl_expires IS NOT NULL",
                   "n.ttl_expires < datetime()",
                   harmful_automatic_mutation_allowed_cypher("n"))
            .where_if(session_id is not None, "n.session_id = $session_id")
            .return_raw("""n.uuid AS uuid,
       n.name AS name,
       n.session_id AS session_id""")
            .build())
        return self.neo4j.execute(query, params=params)
