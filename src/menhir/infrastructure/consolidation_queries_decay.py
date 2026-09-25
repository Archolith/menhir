"""Decay candidate selection, compression, and rehydration queries.

Moved verbatim from ``consolidation_queries.py`` and composed into
``ConsolidationRepository`` via this mixin.
"""

from __future__ import annotations

from menhir.infrastructure.consolidation_queries_common import (
    _DECAY_CANDIDATE_LIMIT,
    automatic_lifecycle_protection_cypher,
    harmful_automatic_mutation_allowed_cypher,
)
from menhir.infrastructure.cypher import Cypher


class ConsolidationDecayCompressionMixin:
    """Decay and compression half of ``ConsolidationRepository``."""

    # -------------------------------------------------------------------------
    # Decay / compression
    # -------------------------------------------------------------------------

    def fetch_decay_candidates(
        self,
        freshness: str,
        *,
        min_days_since_accessed: float,
        max_edge_count: int,
        max_sharpness: float | None = None,
        limit: int = _DECAY_CANDIDATE_LIMIT,
    ) -> list[dict[str, object]]:
        """Fetch PERSISTENT decay candidates using current cached graph signals.

        Sharpness filtering:
        - Compress phase (max_sharpness=None): include nodes with null sharpness
          for inline recomputation before deciding.
        - Delete phase (max_sharpness set): exclude null sharpness (protective gate:
          unknown uniqueness never justifies deletion).
        """

        query = (Cypher()
            .match("(n:Entity)")
            .where("n.scope = 'PERSISTENT'",
                   "n.freshness = $freshness",
                   harmful_automatic_mutation_allowed_cypher("n"),
                   "coalesce(n.last_accessed, n.created_at) < datetime() - duration({days: $min_days_since_accessed})",
                   "coalesce(toInteger(n.edge_count), 0) < $max_edge_count")
            .where_if(max_sharpness is not None, "n.sharpness IS NOT NULL AND toFloat(n.sharpness) < $max_sharpness")
            .return_raw("""n.uuid AS uuid,
       n.type AS type,
       n.name AS name,
       n.content AS content,
       n.summary AS summary,
       n.scope AS scope,
       n.sharpness AS sharpness,
       n.edge_count AS edge_count,
       n.freshness AS freshness,
       n.user_flagged AS user_flagged,
       n.last_accessed AS last_accessed,
       coalesce(n.namespace, 'default') AS namespace,
       coalesce(toInteger(n.rehydration_count), 0) AS rehydration_count,
       CASE WHEN n.target_date IS NOT NULL AND date(n.target_date) < date() THEN true ELSE false END AS target_date_passed,
       n.created_at AS created_at""")
            .order_by("coalesce(n.created_at, n.last_accessed) ASC, n.uuid")
            .limit()
            .build())
        params: dict[str, object] = {
            "freshness": freshness,
            "min_days_since_accessed": min_days_since_accessed,
            "max_edge_count": max_edge_count,
            "limit": limit,
        }
        if max_sharpness is not None:
            params["max_sharpness"] = max_sharpness
        return self.neo4j.execute(query, params=params)

    def compress_node(self, node_uuid: str, compressed_summary: str) -> bool:
        """Transition a PERSISTENT ACTIVE node to COMPRESSED with a summary."""

        query = (Cypher()
            .match("(n:Entity {uuid: $node_uuid})")
            .where("n.scope = 'PERSISTENT'", "n.freshness = 'ACTIVE'",
                   harmful_automatic_mutation_allowed_cypher("n"))
            .set(("n.original_content = coalesce(n.original_content, n.content)",
                  "n.content = $compressed_summary",
                  "n.freshness = 'COMPRESSED'"))
            .return_raw("count(n) AS updated")
            .build())
        rows = self.neo4j.execute(query, params={
                "node_uuid": node_uuid,
                "compressed_summary": compressed_summary,
            })
        return bool(rows and int(rows[0].get("updated", 0)) > 0)

    def fetch_node_freshness(self, node_uuids: list[str]) -> dict[str, str]:
        """Return {uuid: freshness} for the given Entity nodes."""
        if not node_uuids:
            return {}
        query = (Cypher()
            .match("(n:Entity)")
            .where("n.uuid IN $uuids")
            .return_raw("n.uuid AS uuid, coalesce(n.freshness, 'ACTIVE') AS freshness")
            .build())
        rows = self.neo4j.execute(query, params={"uuids": list(dict.fromkeys(node_uuids))})
        return {
            str(row["uuid"]): str(row.get("freshness") or "ACTIVE")
            for row in rows
            if row.get("uuid")
        }

    def complete_rehydration(self, node_uuid: str, updated_content: str | None = None) -> bool:
        """Transition a PERSISTENT COMPRESSED node back to ACTIVE."""
        params: dict[str, object] = {"node_uuid": node_uuid}
        set_lines = [
            "n.freshness = 'ACTIVE'",
            "n.last_accessed = datetime()",
            "n.rehydration_count = coalesce(toInteger(n.rehydration_count), 0) + 1",
        ]
        if updated_content is not None:
            params["updated_content"] = updated_content
            set_lines.insert(0, "n.content = $updated_content")

        query = (Cypher()
            .match("(n:Entity {uuid: $node_uuid})")
            .where("n.scope = 'PERSISTENT'", "n.freshness = 'COMPRESSED'",
                   automatic_lifecycle_protection_cypher("n"))
            .set(set_lines)
            .return_raw("count(n) AS updated")
            .build())
        rows = self.neo4j.execute(query, params=params)
        return bool(rows and int(rows[0].get("updated", 0)) > 0)

    def bridge_and_delete(self, node_uuid: str) -> dict[str, int]:
        """Bridge entity neighbors around a node, then delete it.

        Excludes ANCHORED_TO edges and structural neighbors from bridging
        so cross-domain anchor links don't create spurious semantic bridges.
        """

        query = """
            MATCH (n:Entity {uuid: $node_uuid})
            WHERE __NON_DERIVED_VIEW__
            CALL {
                WITH n
                OPTIONAL MATCH (n)-[r]-(neighbor:Entity)
                WHERE NOT type(r) = 'ANCHORED_TO'
                  AND neighbor.structure_role IS NULL
                WITH collect(DISTINCT neighbor) AS neighbors
                UNWIND neighbors AS a
                UNWIND neighbors AS b
                WITH a, b WHERE a.uuid < b.uuid
                MERGE (a)-[r:RELATES_TO]->(b)
                ON CREATE SET r.type = 'bridged',
                              r.bridged_from = $node_uuid,
                              r.weight = 1.0,
                              r.source = 'system-derived',
                              r.scope = 'PERSISTENT',
                              r.created_at = datetime(),
                              r.last_traversed = datetime()
                RETURN count(*) AS edges_bridged
            }
            WITH n, coalesce(edges_bridged, 0) AS edges_bridged
            DETACH DELETE n
            RETURN edges_bridged AS edges_bridged, 1 AS deleted
            """.replace(
                "__NON_DERIVED_VIEW__", harmful_automatic_mutation_allowed_cypher("n")
            )
        rows = self.neo4j.execute(
            query,
            params={"node_uuid": node_uuid},
        )
        if not rows:
            return {"edges_bridged": 0, "deleted": 0}
        row = rows[0]
        return {
            "edges_bridged": int(row.get("edges_bridged", 0) or 0),
            "deleted": int(row.get("deleted", 0) or 0),
        }

    def update_sharpness(self, node_uuid: str, sharpness: float) -> bool:
        """Write computed sharpness to a node."""

        query = (Cypher()
            .match("(n:Entity)")
            .where("n.uuid = $node_uuid", automatic_lifecycle_protection_cypher("n"))
            .set("n.sharpness = $sharpness")
            .return_raw("count(n) AS updated")
            .build())
        rows = self.neo4j.execute(query, params={"node_uuid": node_uuid, "sharpness": sharpness})
        return bool(rows and int(rows[0].get("updated", 0)) > 0)
