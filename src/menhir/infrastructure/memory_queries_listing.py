"""Overview and listing queries for the memory read repository.

Split from ``memory_queries.py``; method bodies are verbatim, composed into
``MemoryQueryRepository`` by the facade module.
"""

from __future__ import annotations

import hashlib
from typing import Any

from menhir.domain.bootstrap_scope import bootstrap_selection
from menhir.domain.namespace import namespace_spellings, namespace_to_group_ids
from menhir.domain.recall_visibility import default_recall_visibility_cypher
from menhir.domain.structural_memory import non_structural_memory_cypher
from menhir.infrastructure.cypher import Cypher, MEMORY_RETURN_FIELDS
from menhir.infrastructure.memory_queries_admission import admission_provenance_state


class MemoryQueryListingMixin:
    """Overview and listing methods of ``MemoryQueryRepository``."""

    # --- Overview & listing -------------------------------------------------

    def fetch_memory_overview(self, namespace: str | None = None) -> dict[str, Any]:
        """Return high-level graph counts, optionally scoped to one silo (CF-33).

        ``namespace=None`` counts every silo and is the default, because most callers of this are
        operational -- the scheduler's queue-health job and the metadata resource want the whole
        deployment. `get_memory_stats` passes the caller's (pinned) namespace so a tenant sees its
        own cardinality rather than the graph's.

        Scoping goes through the two domain helpers, never a predicate spelled here: ``:Entity`` /
        ``:Episodic`` match on graphiti's ``group_id`` (``namespace_to_group_ids``) while
        ``:TurnEvidence`` matches on its own ``namespace`` property (``namespace_spellings``).
        Both encode the same owner ruling, 2026-08-21: ``''`` and ``'default'`` are the SAME silo.

        Includes the admission-provenance pair (CF-229). `:TurnEvidence` capture and the
        `ADMITTED_ON` join it feeds are written by DIFFERENT callers, so the join can be dead while
        every unit test, every E2E and every health surface stays green -- which is exactly what
        happened: 576 turns captured, 0 edges drawn, and nothing said so. The two counts are
        reported side by side because neither is meaningful alone; it is their RATIO that reveals a
        producer that captures turns and never reports the pairing.
        """

        group_ids = namespace_to_group_ids(namespace)
        params: dict[str, Any] = {}
        node_filter = ""
        if group_ids is not None:
            node_filter = " AND n.group_id IN $group_ids"
            params["group_ids"] = group_ids

        rows = self.neo4j.execute(
            f"""
            MATCH (n)
            WHERE (n:Entity OR n:Episodic){node_filter}
            RETURN count(n) AS total_memories,
                   count(CASE WHEN n:Entity THEN 1 END) AS entity_count,
                   count(CASE WHEN n:Episodic THEN 1 END) AS episode_count,
                   count(CASE WHEN coalesce(n.user_flagged, false) THEN 1 END) AS flagged_count,
                   count(CASE WHEN n.scope = 'SESSION' THEN 1 END) AS session_count,
                   count(CASE WHEN n.scope = 'PERSISTENT' THEN 1 END) AS persistent_count,
                   count(CASE WHEN n.scope = 'PROMOTED' THEN 1 END) AS promoted_count,
                   count(CASE WHEN n:Episodic AND n.processing_state = 'PENDING' THEN 1 END) AS pending_count,
                   count(CASE WHEN n:Episodic AND n.processing_state = 'ENRICHING' THEN 1 END) AS enriching_count,
                   count(CASE WHEN n:Episodic AND n.processing_state = 'READY' THEN 1 END) AS ready_count,
                   count(CASE WHEN n:Episodic AND n.processing_state = 'FAILED' THEN 1 END) AS failed_count
            """,
            params or None,
        )
        # Separate statement: :TurnEvidence is neither :Entity nor :Episodic, so it cannot be
        # folded into the CASE aggregation above. Both counts come from label/type indexes.
        #
        # Scoped on `namespace`, NOT `group_id`: :TurnEvidence carries no group_id at all
        # (verified on the live graph -- 576 nodes, group_id absent on every one), so a group_id
        # predicate here would silently count zero and report a false `never_linked`.
        turn_spellings = namespace_spellings(namespace)
        admission_params: dict[str, Any] = {}
        turn_filter = edge_filter = ""
        if turn_spellings is not None:
            turn_filter = " WHERE t.namespace IN $ns"
            edge_filter = " WHERE t.namespace IN $ns"
            admission_params["ns"] = turn_spellings
        admission_rows = self.neo4j.execute(
            f"""
            CALL () {{ MATCH (t:TurnEvidence){turn_filter} RETURN count(t) AS turns }}
            CALL () {{ MATCH ()-[r:ADMITTED_ON]->(t:TurnEvidence){edge_filter} RETURN count(r) AS edges }}
            RETURN turns AS turn_evidence_count, edges AS admission_edge_count
            """,
            admission_params or None,
        )
        admission = admission_rows[0] if admission_rows else {}
        overview = dict(rows[0]) if rows else {
            "total_memories": 0,
            "entity_count": 0,
            "episode_count": 0,
            "flagged_count": 0,
            "session_count": 0,
            "persistent_count": 0,
            "promoted_count": 0,
            "pending_count": 0,
            "enriching_count": 0,
            "ready_count": 0,
            "failed_count": 0,
        }
        overview["turn_evidence_count"] = int(admission.get("turn_evidence_count") or 0)
        overview["admission_edge_count"] = int(admission.get("admission_edge_count") or 0)
        overview["admission_provenance"] = admission_provenance_state(
            turn_evidence_count=overview["turn_evidence_count"],
            admission_edge_count=overview["admission_edge_count"],
        )
        return overview

    def fetch_recent_memories(
        self, limit: int = 10, namespace: str | None = None
    ) -> list[dict[str, Any]]:
        """Return the most recently accessed or created memory nodes."""

        safe_limit = max(1, min(limit, 50))
        where = [
            "(n:Entity OR n:Episodic)",
            non_structural_memory_cypher("n"),
            default_recall_visibility_cypher("n"),
        ]
        params: dict[str, Any] = {"limit": safe_limit}
        if namespace is not None and str(namespace).strip():
            where.append("coalesce(n.namespace, 'default') = $namespace")
            params["namespace"] = str(namespace).strip()
        query = (Cypher()
            .match("(n)")
            .where(*where)
            .return_fields(MEMORY_RETURN_FIELDS)
            .order_by("coalesce(n.last_accessed, n.created_at) DESC, n.uuid")
            .limit()
            .build())
        return self.neo4j.execute(query, params=params)

    def fetch_flagged_memories(
        self,
        limit: int = 10,
        workspace: str | None = None,
        *,
        namespace: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return explicitly flagged memory nodes for bootstrap context reads.

        Excludes structural graph nodes (project-scan entities with structure_role)
        because they are not useful as bootstrap context and waste tokens.
        """

        safe_limit = max(1, min(limit, 50))
        _selection_key, allowed_scopes = bootstrap_selection(workspace)
        where = ["(n:Entity OR n:Episodic)",
                 "coalesce(n.user_flagged, false) = true",
                 "n.bootstrap_scope IN $allowed_scopes",
                 non_structural_memory_cypher("n"),
                 default_recall_visibility_cypher("n")]
        params: dict[str, Any] = {
            "limit": safe_limit,
            "allowed_scopes": allowed_scopes,
        }
        group_ids = namespace_to_group_ids(namespace)
        if group_ids is not None:
            where.append("n.group_id IN $group_ids")
            params["group_ids"] = group_ids
        query = (Cypher()
            .match("(n)")
            .where(*where)
            .return_fields(MEMORY_RETURN_FIELDS)
            .order_by("coalesce(n.last_accessed, n.created_at) DESC, n.uuid")
            .limit()
            .build())
        return self.neo4j.execute(query, params=params)

    def fetch_flagged_memory_bootstrap_version(
        self,
        workspace: str | None = None,
        *,
        namespace: str | None = None,
    ) -> str:
        """Return a deterministic version fingerprint for the flagged-memory set.

        Excludes structural graph nodes so the version only changes when
        semantic flagged memories change.
        """

        selection_key, allowed_scopes = bootstrap_selection(workspace)
        where = ["(n:Entity OR n:Episodic)",
                 "coalesce(n.user_flagged, false) = true",
                 "n.bootstrap_scope IN $allowed_scopes",
                 non_structural_memory_cypher("n"),
                 default_recall_visibility_cypher("n")]
        params: dict[str, Any] = {"allowed_scopes": allowed_scopes}
        group_ids = namespace_to_group_ids(namespace)
        if group_ids is not None:
            where.append("n.group_id IN $group_ids")
            params["group_ids"] = group_ids
        rows = self.neo4j.execute(
            f"""
            MATCH (n)
            WHERE {" AND ".join(where)}
            WITH n ORDER BY n.uuid
            RETURN collect(n.uuid) AS uuids
            """,
            params=params,
        )
        uuids = [str(uuid) for uuid in (rows[0].get("uuids", []) if rows else []) if uuid]
        digest = hashlib.sha256("|".join(uuids).encode("utf-8")).hexdigest()[:16]
        return f"{selection_key}:{len(uuids)}:{digest}"

    def fetch_memory_by_uuid(
        self, node_uuid: str, *, namespace: str | None = None
    ) -> dict[str, Any] | None:
        """Return a single memory node by UUID, optionally restricted to one namespace."""

        where = ["(n:Entity OR n:Episodic)", "n.uuid = $node_uuid"]
        params: dict[str, Any] = {"node_uuid": node_uuid}
        if namespace is not None and str(namespace).strip():
            where.append("coalesce(n.namespace, 'default') = $namespace")
            params["namespace"] = str(namespace).strip()
        query = (Cypher()
            .match("(n)")
            .where(*where)
            .return_fields(MEMORY_RETURN_FIELDS)
            .limit("1")
            .build())
        rows = self.neo4j.execute(query, params=params)
        return rows[0] if rows else None

    def fetch_memories_by_scope(
        self, scope: str, limit: int = 10, *, namespace: str | None = None
    ) -> list[dict[str, Any]]:
        """Return memory nodes filtered by scope, optionally restricted to one namespace."""

        safe_limit = max(1, min(limit, 50))
        where = [
            "(n:Entity OR n:Episodic)",
            "n.scope = $scope",
            default_recall_visibility_cypher("n"),
        ]
        params: dict[str, Any] = {"scope": scope, "limit": safe_limit}
        if namespace is not None and str(namespace).strip():
            where.append("coalesce(n.namespace, 'default') = $namespace")
            params["namespace"] = str(namespace).strip()
        query = (Cypher()
            .match("(n)")
            .where(*where)
            .return_fields(MEMORY_RETURN_FIELDS)
            .order_by("coalesce(n.last_accessed, n.created_at) DESC, n.uuid")
            .limit()
            .build())
        return self.neo4j.execute(query, params=params)

    def fetch_memories_by_type(
        self, memory_type: str, limit: int = 10, *, namespace: str | None = None
    ) -> list[dict[str, Any]]:
        """Return entity memories filtered by type, optionally restricted to one namespace."""

        safe_limit = max(1, min(limit, 50))
        where = ["n.type = $memory_type", default_recall_visibility_cypher("n")]
        params: dict[str, Any] = {"memory_type": memory_type, "limit": safe_limit}
        if namespace is not None and str(namespace).strip():
            where.append("coalesce(n.namespace, 'default') = $namespace")
            params["namespace"] = str(namespace).strip()
        query = (Cypher()
            .match("(n:Entity)")
            .where(*where)
            .return_fields(MEMORY_RETURN_FIELDS)
            .order_by("coalesce(n.last_accessed, n.created_at) DESC, n.uuid")
            .limit()
            .build())
        return self.neo4j.execute(query, params=params)
