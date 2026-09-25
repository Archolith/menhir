"""Recall-scoring helper queries for the memory read repository.

Split from ``memory_queries.py``; method bodies are verbatim, composed into
``MemoryQueryRepository`` by the facade module.
"""

from __future__ import annotations

from menhir.domain.recall import adjacency_edge_pattern
from menhir.domain.structural_memory import non_structural_memory_cypher
from menhir.infrastructure.cypher import (
    Cypher,
    ENTITY_METADATA_FIELDS,
    FACT_TEMPORAL_FIELDS,
    SHADOW_CANDIDATE_FACT_EDGE_FIELDS,
)


class MemoryQueryRecallMixin:
    """Recall-scoring helper methods of ``MemoryQueryRepository``."""

    # --- Recall scoring helpers ---------------------------------------------

    def search_content_embeddings(
        self,
        query_vector: list[float],
        *,
        limit: int = 50,
        group_ids: list[str] | None = None,
    ) -> list[dict[str, object]]:
        """Return content-embedding cosine hits without mutating graph state."""
        safe_limit = max(1, min(limit, 500))
        return self.neo4j.execute(
            """
            MATCH (n:Entity)
            WHERE n.content_embedding IS NOT NULL
              AND ($group_ids IS NULL OR n.group_id IN $group_ids)
            WITH n, vector.similarity.cosine(n.content_embedding, $query_vector) AS cosine
            WHERE cosine IS NOT NULL
            RETURN n.uuid AS uuid, n.name AS name, cosine
            ORDER BY cosine DESC, n.uuid
            LIMIT $limit
            """,
            params={
                "query_vector": query_vector,
                "group_ids": group_ids,
                "limit": safe_limit,
            },
        )

    def search_assertion_embeddings(
        self,
        query_vector: list[float],
        *,
        limit: int = 50,
        namespaces: list[str] | None = None,
    ) -> list[dict[str, object]]:
        """Cosine hits over `:TypedAssertion` observation embeddings (Phase 4a.2 observation lane).

        The recall pipeline is otherwise `:Entity`-only (`fetch_candidate_metadata` matches `(n:Entity)`),
        so a typed-scalar observation is never searched. This is the observation lane's candidate search,
        mirroring `search_content_embeddings` but over the assertion log. Only MATERIALIZABLE observations
        are candidates -- current (`NOT superseded`) and bound (`NOT binding_pending`); a superseded or
        unbound row must never surface. Scoped by `namespace` (assertions carry `namespace`, not
        `group_id`). Returns the assertion id + `stated_span` (the recall surface / user's own words) +
        cosine + the slot/subject identity a downstream deterministic View-authority lookup needs."""
        safe_limit = max(1, min(limit, 500))
        return self.neo4j.execute(
            """
            MATCH (a:TypedAssertion)
            WHERE a.name_embedding IS NOT NULL
              AND NOT coalesce(a.superseded, false)
              AND NOT coalesce(a.binding_pending, false)
              AND ($namespaces IS NULL OR a.namespace IN $namespaces)
            WITH a, vector.similarity.cosine(a.name_embedding, $query_vector) AS cosine
            WHERE cosine IS NOT NULL
            RETURN a.assertion_id AS assertion_id, a.stated_span AS stated_span, cosine,
                   a.subject_uuid AS subject_uuid, a.subject_display AS subject_display,
                   a.attribute AS attribute, a.scope AS scope, a.value_kind AS value_kind,
                   a.unit AS unit, a.namespace AS namespace, a.evidence_tier AS evidence_tier,
                   a.operation AS operation, a.value AS value, toString(a.valid_at) AS valid_at
            ORDER BY cosine DESC, a.assertion_id
            LIMIT $limit
            """,
            params={
                "query_vector": query_vector,
                "namespaces": namespaces,
                "limit": safe_limit,
            },
        )

    def fetch_assertion_candidate_metadata(
        self, assertion_ids: list[str]
    ) -> list[dict[str, object]]:
        """Hydrate scoring/display fields for surfaced `:TypedAssertion` observations by id (Phase 4a.2).

        The Entity hydration path (`fetch_candidate_metadata`) matches `(n:Entity)` only, so an
        observation candidate would be dropped as `meta is None`. This is the parallel non-Entity
        hydration keyed by `assertion_id`. Returns only MATERIALIZABLE rows (current + bound), so a row
        that was superseded between search and hydrate is dropped rather than surfaced stale."""
        if not assertion_ids:
            return []
        return self.neo4j.execute(
            """
            MATCH (a:TypedAssertion)
            WHERE a.assertion_id IN $ids
              AND NOT coalesce(a.superseded, false)
              AND NOT coalesce(a.binding_pending, false)
            RETURN a.assertion_id AS assertion_id, a.stated_span AS stated_span,
                   a.subject_uuid AS subject_uuid, a.subject_display AS subject_display,
                   a.attribute AS attribute, a.scope AS scope, a.value_kind AS value_kind,
                   a.unit AS unit, a.namespace AS namespace, a.evidence_tier AS evidence_tier,
                   a.operation AS operation, a.value AS value, toString(a.valid_at) AS valid_at
            """,
            params={"ids": list(assertion_ids)},
        )

    def fetch_candidate_metadata(self, node_uuids: list[str]) -> list[dict[str, object]]:
        """Fetch scoring-relevant fields for candidate nodes."""
        if not node_uuids:
            return []
        # belief_commit is an optional frontier-era property. Parameterized dynamic
        # access avoids a Neo4j "property key does not exist" warning on legacy graphs
        # where the token has never been created, while preserving a normal null value.
        metadata_fields = tuple(
            "n[$belief_commit_key] AS belief_commit"
            if field == "n.belief_commit AS belief_commit"
            else field
            for field in ENTITY_METADATA_FIELDS
        )
        query = (Cypher()
            .match("(n:Entity)")
            .where("n.uuid IN $uuids")
            .return_fields(metadata_fields)
            .build())
        return self.neo4j.execute(
            query,
            params={"uuids": node_uuids, "belief_commit_key": "belief_commit"},
        )

    def fetch_temporal_facts(self, node_uuids: list[str]) -> list[dict[str, object]]:
        """Fetch bi-temporal fact-edge state for candidate entity nodes."""
        if not node_uuids:
            return []
        query = (Cypher()
            .match("(n:Entity)-[r:RELATES_TO]-(m:Entity)")
            .where("n.uuid IN $uuids")
            .return_fields(FACT_TEMPORAL_FIELDS)
            .build())
        return self.neo4j.execute(query, params={"uuids": node_uuids})

    def fetch_candidate_fact_edges(self, node_uuids: list[str]) -> list[dict[str, object]]:
        """Fetch fact-EDGE identity (not just node state) for candidate entity nodes.

        Stage 1 of shadow-mode context composition
        (.agent/plans/menhir-context-composition-production-integration.md) selects at
        fact-edge granularity, not entity granularity -- one entity can carry many
        competing fact-edges (e.g. "Rachel moved to Chicago" / "Rachel moved to Austin" /
        "Rachel moved to the suburbs" are three distinct RELATES_TO edges on the same
        Rachel node). fetch_temporal_facts() above returns only node_uuid + fact text +
        timestamps, which collapses those three into indistinguishable rows under one
        node_uuid. This returns the edge's own uuid plus both endpoints' uuid/name so a
        caller can tell which specific claim was selected.
        """
        if not node_uuids:
            return []
        query = (Cypher()
            .match("(n:Entity)-[r:RELATES_TO]-(m:Entity)")
            .where("n.uuid IN $uuids")
            .return_fields(SHADOW_CANDIDATE_FACT_EDGE_FIELDS)
            .build())
        return self.neo4j.execute(query, params={"uuids": node_uuids})

    def fetch_candidate_provenance(self, node_uuids: list[str]) -> list[dict[str, object]]:
        """Raw per-candidate provenance for the frontier oracle/warden path (evidence + scope).

        Unions independent graph signals per node, each as a pattern comprehension so there is
        no cartesian blow-up across them:
          - first-class ``SUPPORTED_BY`` :Evidence kinds (the L4 provenance model),
          - the ``structure_project`` of every structural node the candidate is ``ANCHORED_TO``
            (drives both the ``file`` evidence anchor and the candidate's project scope),
          - the ``structure_path`` of every structural node the candidate is ``ANCHORED_TO``
            (provides the file path for each anchor),
          - the ``source`` of episodes that ``MENTIONS`` the node.
        Mapping source labels -> evidence kinds, the project pick, and the final union are
        applied in the service layer (domain policy), not here -- raw signals only."""
        if not node_uuids:
            return []
        query = """
            MATCH (n:Entity) WHERE n.uuid IN $uuids
            RETURN n.uuid AS uuid,
                   [ (n)-[supported_by]->(ev:Evidence)
                     WHERE type(supported_by) = $supported_by_type
                     | ev[$evidence_kind_key] ] AS evidence_node_kinds,
                   [ (n)-[:ANCHORED_TO]->(st:Entity)
                     WHERE st.structure_role IS NOT NULL | st.structure_project ] AS anchor_projects,
                   [ (n)-[:ANCHORED_TO]->(st:Entity)
                     WHERE st.structure_role IS NOT NULL | st.structure_path ] AS anchor_paths,
                   [ (epi:Episodic)-[:MENTIONS]->(n) | epi.source ] AS episode_sources
        """
        return self.neo4j.execute(
            query,
            params={
                "uuids": node_uuids,
                "supported_by_type": "SUPPORTED_BY",
                "evidence_kind_key": "kind",
            },
        )

    def fetch_node_receipts(self, node_uuid: str) -> dict[str, object] | None:
        """Receipts for one node (the "show me the sources" read): the source episodes that
        ``MENTIONS`` it, its first-class ``SUPPORTED_BY`` :Evidence, and its ``ANCHORED_TO``
        structural paths. Returns None when no node has that uuid.

        Each episode also carries ``episode_id``: the uuid of Menhir's receipt node for that
        write (#92). The node that MENTIONS an entity is the one Graphiti mints inside
        ``add_episode``; the ``episode_id`` a caller was handed names Menhir's own anchor, which
        records the Graphiti uuid as ``resolved_episode_uuid`` when enrichment completes. Without
        the reverse lookup here, provenance names a uuid the caller has never seen and cannot
        match to its receipt. ``episode_id`` is null for episodes enriched before the anchor
        recorded it, or by a path that does not.

        The receipt lookup runs once per mentioning episode and is re-aggregated before the
        evidence/anchor comprehensions, so the three signals stay independent (no cartesian
        blow-up). ``episodic_resolved_episode_uuid_idx`` backs the lookup; without it this is a
        label scan per episode."""
        query = """
            MATCH (n:Entity {uuid: $uuid})
            OPTIONAL MATCH (epi:Episodic)-[:MENTIONS]->(n)
            OPTIONAL MATCH (receipt:Episodic)
              WHERE epi IS NOT NULL AND receipt.resolved_episode_uuid = epi.uuid
            WITH n,
                 [ e IN collect(
                     CASE WHEN epi IS NULL THEN null ELSE
                       {uuid: epi.uuid, source: epi.source, content: epi.content,
                        created_at: epi.created_at, episode_id: receipt.uuid}
                     END
                   ) WHERE e IS NOT NULL ] AS episodes
            RETURN n.uuid AS uuid,
                   n.name AS name,
                   n.view_kind AS view_kind,
                   episodes,
                   [ (n)-[supported_by]->(ev:Evidence)
                     WHERE type(supported_by) = $supported_by_type
                     | {kind: ev[$evidence_kind_key], ref: ev[$evidence_ref_key]} ] AS evidence,
                   [ (n)-[:ANCHORED_TO]->(st:Entity)
                     WHERE st.structure_role IS NOT NULL | st.structure_path ] AS anchor_paths
        """
        rows = self.neo4j.execute(
            query,
            params={
                "uuid": node_uuid,
                "supported_by_type": "SUPPORTED_BY",
                "evidence_kind_key": "kind",
                "evidence_ref_key": "ref",
            },
        )
        return dict(rows[0]) if rows else None

    def fetch_adjacency_pairs(
        self,
        candidate_uuids: list[str],
        context_uuids: list[str] | None = None,
        namespace: str | None = None,
    ) -> list[dict[str, object]]:
        """Find edges connecting candidates to each other or to context nodes.

        When namespace is provided, constrains the adjacency traversal so only
        same-namespace nodes participate. When None, behavior is unchanged.
        """
        all_uuids = list(dict.fromkeys(candidate_uuids + (context_uuids or [])))
        if len(all_uuids) < 2:
            return []
        # CF-247: TYPED, from the domain's `ADJACENCY_EDGE_TYPES`. This was `-[r]-`, which made the
        # contract "every relationship type in the graph establishes adjacency" -- not a decision
        # anyone recorded, just what an untyped pattern gives you. The consumer
        # (`increment_edge_weights`) emits the SAME list: narrowing one alone would let recall rank
        # on an edge it then declines to reinforce.
        query = f"""
            MATCH (a)-[r:{adjacency_edge_pattern()}]-(b)
            WHERE a.uuid IN $all_uuids AND b.uuid IN $all_uuids
              AND a.uuid <> b.uuid
        """
        if namespace is not None:
            query += """
              AND coalesce(a.namespace, 'default') = $namespace
              AND coalesce(b.namespace, 'default') = $namespace
        """
        query += """
            RETURN DISTINCT a.uuid AS source, b.uuid AS target,
                   r.weight AS weight, r.uuid AS edge_uuid
            """
        params = {"all_uuids": all_uuids}
        if namespace is not None:
            params["namespace"] = namespace
        return self.neo4j.execute(query, params=params)

    def touch_retrieved_nodes(self, node_uuids: list[str]) -> int:
        """Update last_accessed for retrieved nodes, return count touched."""
        if not node_uuids:
            return 0
        rows = self.neo4j.execute(
            """
            MATCH (n:Entity)
            WHERE n.uuid IN $uuids
            SET n.last_accessed = datetime()
            RETURN count(n) AS touched
            """,
            params={"uuids": node_uuids},
        )
        return int(rows[0].get("touched", 0)) if rows else 0

    def unflag_structural_nodes(self) -> int:
        """One-time cleanup: remove user_flagged from structural graph nodes.

        These were accidentally flagged in earlier sessions. Structural nodes
        (project, directory, file, symbol, etc.) are not useful as bootstrap
        context and waste tokens.
        """
        rows = self.neo4j.execute(
            f"""
            MATCH (n:Entity)
            WHERE coalesce(n.user_flagged, false) = true
              AND NOT ({non_structural_memory_cypher("n")})
            SET n.user_flagged = false,
                n.bootstrap_scope = null
            RETURN count(n) AS unflagged
            """
        )
        return int(rows[0].get("unflagged", 0)) if rows else 0
