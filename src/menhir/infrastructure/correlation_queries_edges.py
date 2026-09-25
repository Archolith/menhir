"""RELATES_TO correlation-edge queries.

Moved verbatim from ``correlation_queries.py`` and composed into
``CorrelationRepository`` via this mixin.
"""

from __future__ import annotations


class CorrelationEdgeMixin:
    """RELATES_TO half of ``CorrelationRepository``: edge creation and lookup."""

    # ------------------------------------------------------------------
    # RELATES_TO edge creation
    # ------------------------------------------------------------------

    def create_related_to_edge(
        self,
        source_uuid: str,
        target_uuid: str,
        *,
        similarity: float,
        source: str = "correlation-detected",
    ) -> bool:
        """Create a RELATES_TO edge between two entities if none exists.

        Idempotent — MERGE avoids duplicate edges.  Returns True if an edge
        was created (first time); False if it already existed.
        """
        rows = self._neo4j.execute(
            """
            MATCH (a:Entity {uuid: $source_uuid})
            MATCH (b:Entity {uuid: $target_uuid})
            MERGE (a)-[r:RELATES_TO]->(b)
            ON CREATE SET
                r.type = 'correlation',
                r.similarity = $similarity,
                r.source = $source,
                r.scope = 'PERSISTENT',
                r.weight = $similarity,
                r.created_at = datetime(),
                r.last_traversed = datetime()
            ON MATCH SET
                r.similarity = CASE
                    WHEN $similarity > r.similarity THEN $similarity
                    ELSE r.similarity
                END,
                r.weight = CASE
                    WHEN $similarity > r.weight THEN $similarity
                    ELSE r.weight
                END,
                r.last_traversed = datetime()
            RETURN r.type AS edge_type
            """,
            params={
                "source_uuid": source_uuid,
                "target_uuid": target_uuid,
                "similarity": similarity,
                "source": source,
            },
        )
        return bool(rows)

    # ------------------------------------------------------------------
    # Correlation lookup — check if a RELATES_TO edge already exists
    # ------------------------------------------------------------------

    def correlation_exists(self, uuid_a: str, uuid_b: str) -> bool:
        """Check whether a RELATES_TO edge exists between two entities."""
        rows = self._neo4j.execute(
            """
            MATCH (a:Entity {uuid: $a})-[r:RELATES_TO]-(b:Entity {uuid: $b})
            WHERE r.type = 'correlation'
            RETURN count(r) AS cnt
            """,
            params={"a": uuid_a, "b": uuid_b},
        )
        return bool(rows and int(rows[0].get("cnt", 0)) > 0)
