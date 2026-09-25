"""Entity edge-count and traversal-weight maintenance queries.

Moved verbatim from ``consolidation_queries.py`` and composed into
``ConsolidationRepository`` via this mixin.
"""

from __future__ import annotations

from menhir.domain.recall import adjacency_edge_pattern


class ConsolidationGraphMaintenanceMixin:
    """Graph maintenance half of ``ConsolidationRepository``: edge counts and weights."""

    # -------------------------------------------------------------------------
    # Graph maintenance
    # -------------------------------------------------------------------------

    def sync_edge_counts(self) -> int:
        """Bulk recount entity edge_count values from the current graph.

        Excludes ANCHORED_TO edges so structural cross-links don't inflate
        decay-protection thresholds.

        Scaling note: this touches every Entity node in a single transaction.
        Fine for personal-scale graphs (hundreds to low thousands of nodes).
        If the graph reaches tens of thousands of densely-connected entities,
        consider migrating to CALL {} IN TRANSACTIONS for batched writes.
        """

        rows = self.neo4j.execute(
            """
            MATCH (n:Entity)
            OPTIONAL MATCH (n)-[r]-()
            WHERE NOT type(r) = 'ANCHORED_TO'
            WITH n, count(DISTINCT r) AS edge_count
            SET n.edge_count = edge_count
            RETURN count(n) AS synced
            """,
        )
        return int(rows[0].get("synced", 0)) if rows else 0

    #: DIRECTED on purpose, and it is a bug fix rather than a style choice.
    #:
    #: This match was `()-[r]-()`, which yields every relationship TWICE -- once per direction --
    #: so the SET ran twice and each call raised the weight by 0.2 against a documented "+0.1 per
    #: traversal", reaching the 5.0 cap in half the traversals the ratchet was designed for.
    #: Confirmed by execution against Neo4j 5, not by reading: weight 1.0 -> 1.2 and
    #: `count(r)` -> 2. `test_edge_weight_cap_contract` could not catch it because it greps this
    #: source for the string "0.1" rather than running the query. `()-[r]->()` matches each
    #: relationship exactly once regardless of type, so it fixes the rate and halves the scan
    #: without narrowing what can be reinforced.
    #:
    #: STILL UNTYPED, deliberately. A typed pattern would let the per-type `uuid` relationship
    #: indexes turn this from an all-relationship scan into an index seek (42,100 -> 351 dbHits at
    #: N=50 on a 21k-edge graph), but `edge_index` is populated by `fetch_adjacency_pairs`, which
    #: is also untyped -- so naming a type list here and not there would silently stop reinforcing
    #: every edge outside the list. See CF-247: the graph has 35+ relationship types, and deciding
    #: which of them earn traversal reinforcement is a contract question, not an optimization.
    def increment_edge_weight(self, edge_uuid: str) -> bool:
        """Increment traversal weight for an edge while capping at the v1 max."""

        rows = self.neo4j.execute(
            f"""
            MATCH ()-[r:{adjacency_edge_pattern()}]->()
            WHERE r.uuid = $edge_uuid
            SET r.weight = CASE
                    WHEN coalesce(toFloat(r.weight), 1.0) < 5.0
                    THEN coalesce(toFloat(r.weight), 1.0) + 0.1
                    ELSE 5.0
                END,
                r.last_traversed = datetime()
            RETURN count(r) AS edges_updated
            """,
            params={"edge_uuid": edge_uuid},
        )
        return bool(rows and int(rows[0].get("edges_updated", 0)) > 0)

    def increment_edge_weights(self, edge_uuids: list[str]) -> int:
        """Ratchet many edges in ONE round trip. Returns the number of edges updated.

        CF-75's recall path called `increment_edge_weight` once per traversed edge, serially, and
        each of those calls is an all-relationship scan. The cost was therefore N scans of the
        whole relationship store: 3,150,400 dbHits for 50 edges on a 21k-edge graph, growing with
        BOTH the result size and the graph. One `IN` collapses that to a single scan (42,100),
        because the scan is per-query, not per-uuid.
        """
        if not edge_uuids:
            return 0
        rows = self.neo4j.execute(
            f"""
            MATCH ()-[r:{adjacency_edge_pattern()}]->()
            WHERE r.uuid IN $edge_uuids
            SET r.weight = CASE
                    WHEN coalesce(toFloat(r.weight), 1.0) < 5.0
                    THEN coalesce(toFloat(r.weight), 1.0) + 0.1
                    ELSE 5.0
                END,
                r.last_traversed = datetime()
            RETURN count(r) AS edges_updated
            """,
            params={"edge_uuids": list(dict.fromkeys(edge_uuids))},
        )
        return int(rows[0].get("edges_updated", 0)) if rows else 0

    def update_edge_facts(self, updates: list[dict[str, str]]) -> int:
        """Bulk-update edge facts and provenance. Returns count updated.

        DIRECTED, for the same reason `increment_edge_weight` above is (CF-75). An anonymous
        undirected match yields every relationship twice -- once per assignment of its two free
        endpoints -- so this returned exactly double the number of edges it had updated. The `SET`
        is idempotent, so no fact was ever corrupted by the second visit; the count was wrong and
        the scan did twice the work it needed to. Confirmed by execution, 2 reported against 1
        real edge, not by reading. CF-250.

        This is the instance the CF-75 fix missed: the hazard was written up 70 lines above, in
        this same file, while its sibling kept the defect. Anchored patterns like `(n)-[r]-(peer)`
        do NOT have this problem -- one endpoint is bound, so each incident relationship is
        yielded once -- which is why every other undirected match in the codebase is fine.
        """
        if not updates:
            return 0
        rows = self.neo4j.execute(
            """
            UNWIND $updates AS update
            MATCH ()-[r]->()
            WHERE r.uuid = update.uuid
            SET r.fact = update.fact, r.fact_source = update.fact_source
            RETURN count(r) AS updated
            """,
            params={"updates": updates},
        )
        return int(rows[0].get("updated", 0)) if rows else 0
