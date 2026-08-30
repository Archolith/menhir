"""Read-path query repository extracted from MemoryGraphAdapter.

Houses memory listing, lookup, flag/delete, and recall-scoring queries.
All methods preserve the exact signatures and return types of the
original MemoryGraphAdapter methods they replace.
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Any
from uuid import uuid4

from menhir.domain.bootstrap_scope import bootstrap_selection, normalize_bootstrap_scope
from menhir.domain.namespace import normalize_namespace, namespace_spellings, namespace_to_group_ids
from menhir.domain.recall_visibility import default_recall_visibility_cypher
from menhir.domain.structural_memory import non_structural_memory_cypher
from menhir.domain.recall import adjacency_edge_pattern
from menhir.infrastructure.cypher import (
    Cypher,
    ENTITY_METADATA_FIELDS,
    FACT_TEMPORAL_FIELDS,
    MEMORY_RETURN_FIELDS,
    SHADOW_CANDIDATE_FACT_EDGE_FIELDS,
)
from menhir.infrastructure.neo4j import Neo4jRepository

logger = logging.getLogger(__name__)

#: Turn capture is running and the admission join has NEVER been drawn (CF-229).
ADMISSION_NEVER_LINKED = "never_linked"
#: Turns captured and at least one join drawn -- the wiring works.
ADMISSION_LINKED = "linked"
#: No turns captured, so there is nothing to pair and nothing to report.
ADMISSION_NO_TURNS = "no_turns"

_OPAQUE_DIGEST_PATTERN = re.compile(r"^[A-Za-z0-9_-]{32,256}$")
_DIGEST_KEY_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def _validated_evidence_tombstone_params(
    *, evidence_digest: str | None, digest_key_id: str | None
) -> dict[str, str] | None:
    """Validate an already-keyed opaque erasure identity without deriving one locally.

    These repositories currently receive only the raw evidence identifier and have no HMAC
    material or caller-supplied digest/key-id fields.  Returning ``None`` when both values are
    absent keeps current public signatures compatible; supplying only one value, or a value that
    is not an opaque token, fails closed.  A future service boundary may call this helper after it
    computes the digest with managed key material.  This helper intentionally never accepts or
    hashes the raw erased identifier.
    """
    digest = str(evidence_digest or "").strip()
    key_id = str(digest_key_id or "").strip()
    if not digest and not key_id:
        return None
    if not digest or not key_id:
        raise ValueError("evidence tombstones require both an opaque digest and digest key id")
    if _OPAQUE_DIGEST_PATTERN.fullmatch(digest) is None:
        raise ValueError("evidence tombstone digest must be an opaque base64url/hex-like token")
    if _DIGEST_KEY_ID_PATTERN.fullmatch(key_id) is None:
        raise ValueError("evidence tombstone digest key id is invalid")
    return {"evidence_digest": digest, "digest_key_id": key_id}


def admission_provenance_state(*, turn_evidence_count: int, admission_edge_count: int) -> str:
    """Classify the `:TurnEvidence` -> `ADMITTED_ON` wiring from two counts (CF-229).

    Deliberately narrow. The only state asserted as broken is `never_linked`: turns exist and NOT
    ONE edge does. That is unambiguous -- a producer captures turns and no caller ever reports the
    pairing -- and it is the state this deployment sat in unnoticed while every test was green.

    A partial ratio is NOT flagged. Not every memory is admitted on a turn, so "fewer edges than
    turns" is the normal, healthy shape and a threshold on it would be a guess dressed as a
    diagnosis. Zero-of-many is the only signal that carries its own proof.
    """
    if turn_evidence_count <= 0:
        return ADMISSION_NO_TURNS
    return ADMISSION_LINKED if admission_edge_count > 0 else ADMISSION_NEVER_LINKED


class MemoryQueryRepository:
    """Encapsulates memory read/query operations and simple mutations (flag, delete)."""

    def __init__(self, neo4j: Neo4jRepository) -> None:
        self.neo4j = neo4j

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
        structural paths. Pattern comprehensions keep the three signals independent (no cartesian
        blow-up). Returns None when no node has that uuid."""
        query = """
            MATCH (n:Entity {uuid: $uuid})
            RETURN n.uuid AS uuid,
                   n.name AS name,
                   n.view_kind AS view_kind,
                   [ (epi:Episodic)-[:MENTIONS]->(n)
                     | {uuid: epi.uuid, source: epi.source, content: epi.content,
                        created_at: epi.created_at} ] AS episodes,
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

    def delete_memory_with_scalar_cascade(
        self, node_uuid: str, *, operation_id: str
    ) -> dict[str, Any]:
        """Delete one memory/observation and its authoritative scalar assertions atomically.

        A surfaced observation is addressed by ``assertion_id``; an Entity/Episodic deletion also
        removes assertions bound to or grounded on that node.  A pending repair receipt is created
        for every affected subject+namespace in the SAME Neo4j transaction as the deletion.  Thus a
        crash can delay View repair, but can never make the work undiscoverable (G15/G20).
        """
        namespace_rows = self.neo4j.execute(
            """
            OPTIONAL MATCH (target)
            WHERE ((target:Entity OR target:Episodic) AND target.uuid = $node_uuid)
               OR (target:TurnEvidence AND target.turn_id = $node_uuid)
               OR (target:TypedAssertion AND (
                   target.assertion_id = $node_uuid
                   OR target.episode_uuid = $node_uuid
                   OR target.subject_uuid = $node_uuid
               ))
            WITH [target IN collect(DISTINCT target) WHERE target IS NOT NULL |
                CASE
                    WHEN trim(toString(coalesce(target.namespace, target.group_id, ''))) = ''
                    THEN 'default'
                    ELSE trim(toString(coalesce(target.namespace, target.group_id)))
                END
            ] AS keys
            UNWIND CASE WHEN size(keys) = 0 THEN [null] ELSE keys END AS namespace_key
            RETURN collect(DISTINCT namespace_key) AS namespace_keys
            """,
            params={"node_uuid": node_uuid},
        )
        namespace_keys = [
            str(value) for value in (
                namespace_rows[0].get("namespace_keys", []) if namespace_rows else []
            ) if value is not None and str(value).strip()
        ]
        if not namespace_keys:
            return self._empty_memory_erasure_result()
        if len(namespace_keys) != 1:
            raise RuntimeError(
                "evidence erasure target resolves to multiple canonical namespaces; refusing "
                "an unfenced cross-namespace mutation"
            )
        return self._delete_memory_with_scalar_cascade_in_namespace(
            node_uuid,
            operation_id=operation_id,
            namespace_key=namespace_keys[0],
        )

    @staticmethod
    def _empty_memory_erasure_result() -> dict[str, Any]:
        return {
            "touched": False,
            "memory_touched": 0,
            "assertions_deleted": 0,
            "heads_deleted": 0,
            "dependent_views_retired": 0,
            "dependent_views_scrubbed": 0,
            "view_repairs_created": 0,
            "watermarks_reset": 0,
            "repairs": [],
        }

    def _delete_memory_with_scalar_cascade_in_namespace(
        self, node_uuid: str, *, operation_id: str, namespace_key: str
    ) -> dict[str, Any]:
        """Apply one erasure after locking and revalidating its preflight namespace."""
        rows = self.neo4j.execute(
            """
            MERGE (f:EvidenceNamespaceFence {namespace_key: $namespace_key})
            ON CREATE SET f.generation = 0, f.created_at = datetime()
            SET f.lock_nonce = $operation_id, f.locked_at = datetime()
            WITH f
            OPTIONAL MATCH (target)
            WHERE ((target:Entity OR target:Episodic) AND target.uuid = $node_uuid)
               OR (target:TurnEvidence AND target.turn_id = $node_uuid)
               OR (target:TypedAssertion AND (
                   target.assertion_id = $node_uuid
                   OR target.episode_uuid = $node_uuid
                   OR target.subject_uuid = $node_uuid
               ))
            WITH f, [candidate IN collect(DISTINCT target) WHERE candidate IS NOT NULL |
                CASE
                    WHEN trim(toString(coalesce(candidate.namespace, candidate.group_id, ''))) = ''
                    THEN 'default'
                    ELSE trim(toString(coalesce(candidate.namespace, candidate.group_id)))
                END
            ] AS actual_namespace_keys
            WHERE size(actual_namespace_keys) > 0
              AND all(actual_key IN actual_namespace_keys WHERE actual_key = f.namespace_key)
            WITH [f] AS fences
            CALL {
                WITH fences
                OPTIONAL MATCH (v:Entity)
                WHERE (coalesce(v.is_view, false)
                       OR coalesce(v.is_quantstate, false)
                       OR v.view_kind IS NOT NULL)
                  AND (
                      $node_uuid IN coalesce(v.episode_uuids, [])
                      OR v.turn_evidence_uuid = $node_uuid
                      OR EXISTS {
                          MATCH (evidence)-[:MENTIONS]->(v)
                          WHERE (evidence:Episodic AND evidence.uuid = $node_uuid)
                             OR (evidence:TurnEvidence AND evidence.turn_id = $node_uuid)
                      }
                  )
                WITH fences, collect(DISTINCT v) AS dependent_views
                WITH fences, dependent_views,
                     [v IN dependent_views
                      WHERE coalesce(v.view_current, v.qs_current, true)
                        AND NOT coalesce(v.retired, false)] AS current_views
                FOREACH (v IN dependent_views |
                    SET v.episode_uuids = [eid IN coalesce(v.episode_uuids, [])
                                           WHERE eid <> $node_uuid],
                        v.supporting_event_count = size([eid IN coalesce(v.episode_uuids, [])
                                                         WHERE eid <> $node_uuid])
                )
                FOREACH (v IN [candidate IN dependent_views
                               WHERE candidate.turn_evidence_uuid = $node_uuid] |
                    REMOVE v.turn_evidence_uuid
                )
                FOREACH (v IN current_views |
                    SET v.view_current = false,
                        v.qs_current = false,
                        v.retired = true,
                        v.retired_reason = 'contributing_evidence_erased',
                        v.expired_at = datetime(),
                        v.last_accessed = datetime()
                    REMOVE v.ss_view_key_current
                )
                WITH fences, dependent_views, current_views,
                     [v IN current_views | {
                         view: v,
                         source_family: CASE
                             WHEN v.view_kind IN ['scalar_state', 'scalar_history']
                             THEN 'typed_scalar_assertions'
                             WHEN v.view_kind = 'timeline' AND EXISTS {
                                 MATCH (v)-[:EVENT_HISTORY_ENTRY]->(:TypedEventAssertion)
                             }
                             THEN 'typed_event_assertions'
                             ELSE 'none'
                         END,
                         reconstructible: CASE
                             WHEN v.view_kind IN ['scalar_state', 'scalar_history'] AND EXISTS {
                                 MATCH (v)-[:CURRENT_ANCHOR|CONTRIBUTED_TO|HISTORY_ENTRY]
                                       ->(source:TypedAssertion)
                                 WHERE NOT (
                                     source.assertion_id = $node_uuid
                                     OR source.episode_uuid = $node_uuid
                                     OR source.subject_uuid = $node_uuid
                                     OR EXISTS {
                                         MATCH (:Episodic {uuid: $node_uuid})-[:ADMITTED_ON]
                                               ->(:TurnEvidence {turn_id: source.episode_uuid})
                                     }
                                 )
                             }
                             THEN true
                             WHEN v.view_kind = 'timeline' AND EXISTS {
                                 MATCH (v)-[:EVENT_HISTORY_ENTRY]->(source:TypedEventAssertion)
                                 WHERE coalesce(source.episode_uuid, '') <> $node_uuid
                                   AND coalesce(source.turn_evidence_uuid, '') <> $node_uuid
                             }
                             THEN true
                             ELSE false
                         END
                     }] AS view_repairs
                FOREACH (repair IN view_repairs |
                    MERGE (rr:ViewProjectionRepair {
                        repair_key: $operation_id + '\u001f' + repair.view.uuid
                    })
                    ON CREATE SET rr.operation_id = $operation_id,
                                  rr.operation_kind = 'EVIDENCE_ERASURE',
                                  rr.view_uuid = repair.view.uuid,
                                  rr.view_key = coalesce(repair.view.view_key, repair.view.qs_key),
                                  rr.view_kind = repair.view.view_kind,
                                  rr.namespace = coalesce(
                                      repair.view.namespace, repair.view.group_id, 'default'),
                                  rr.namespace_key = head(fences).namespace_key,
                                  rr.fence_generation = head(fences).generation,
                                  rr.view_subtype = repair.view.view_subtype,
                                  rr.subject_uuid = repair.view.view_subject_uuid,
                                  rr.predicate = repair.view.view_predicate,
                                  rr.domain = coalesce(repair.view.view_domain, ''),
                                  rr.source_family = repair.source_family,
                                  rr.reconstructible = repair.reconstructible,
                                  rr.remaining_evidence_count = size(
                                      coalesce(repair.view.episode_uuids, [])),
                                  rr.status = CASE
                                      WHEN repair.reconstructible THEN 'pending'
                                      ELSE 'terminal_not_rebuildable'
                                  END,
                                  rr.terminal_reason = CASE
                                      WHEN NOT repair.reconstructible
                                      THEN 'not_rebuildable'
                                      ELSE null
                                  END,
                                  rr.started_at = datetime()
                )
                WITH fences, dependent_views, current_views
                CALL {
                    WITH fences, dependent_views
                    WITH [fence IN fences | fence.namespace_key]
                         + [v IN dependent_views |
                            coalesce(v.namespace, v.group_id, 'default')] AS namespaces
                    UNWIND CASE WHEN size(namespaces) = 0
                                THEN [null] ELSE namespaces END AS ns
                    OPTIONAL MATCH (w)
                    WHERE (w:ConsolidationWatermark OR w:ScalarConsolidationWatermark
                           OR w:EventConsolidationWatermark)
                      AND (coalesce(w.group_id, w.namespace, '') = ns
                           OR (ns = 'default'
                               AND coalesce(w.group_id, w.namespace, '') = ''))
                    WITH collect(DISTINCT w) AS watermarks
                    FOREACH (w IN watermarks | DETACH DELETE w)
                    RETURN size([w IN watermarks WHERE w IS NOT NULL]) AS watermarks_reset
                }
                RETURN size(current_views) AS dependent_views_retired,
                       size(dependent_views) AS dependent_views_scrubbed,
                       size(current_views) AS view_repairs_created,
                       watermarks_reset
            }
            CALL {
                MATCH (a:TypedAssertion)
                WHERE a.assertion_id = $node_uuid
                   OR a.episode_uuid = $node_uuid
                   OR a.subject_uuid = $node_uuid
                   OR EXISTS {
                       MATCH (source_ep:Episodic {uuid: $node_uuid})
                             -[:ADMITTED_ON]->(:TurnEvidence {turn_id: a.episode_uuid})
                   }
                WITH collect(a) AS doomed,
                     collect(DISTINCT {
                         subject_uuid: a.subject_uuid,
                         namespace: a.namespace
                     }) AS repairs,
                     collect(DISTINCT a.source_key) AS source_keys,
                     collect(DISTINCT a.assertion_id) AS assertion_ids
                FOREACH (repair IN repairs |
                    MERGE (rr:ScalarProjectionRepair {
                        repair_key: $operation_id + '\u001fMEMORY_DELETE\u001f'
                                    + coalesce(repair.namespace, '\u0000null') + '\u001f'
                                    + repair.subject_uuid
                    })
                    ON CREATE SET rr.operation_id = $operation_id,
                                  rr.operation_kind = 'MEMORY_DELETE',
                                  rr.namespace = repair.namespace,
                                  rr.subject_uuid = repair.subject_uuid,
                                  rr.status = 'pending', rr.started_at = datetime()
                )
                FOREACH (a IN doomed | DETACH DELETE a)
                WITH repairs, source_keys, assertion_ids, size(doomed) AS assertions_deleted
                OPTIONAL MATCH (rb:AssertionRebind)
                WHERE rb.assertion_id IN assertion_ids
                WITH repairs, source_keys, assertions_deleted, collect(rb) AS stale_rebinds
                FOREACH (rb IN stale_rebinds | DETACH DELETE rb)
                RETURN repairs, source_keys, assertions_deleted
            }
            CALL {
                OPTIONAL MATCH (n)
                WHERE ((n:Entity OR n:Episodic) AND n.uuid = $node_uuid)
                   OR (n:TurnEvidence AND n.turn_id = $node_uuid)
                CALL {
                    WITH n
                    WITH n WHERE n:Entity
                    DETACH DELETE n
                    RETURN 1 AS touched
                    UNION
                    WITH n
                    WITH n WHERE n:Episodic
                      AND coalesce(n.processing_state, '') IN ['PENDING', 'ENRICHING']
                    SET n.processing_state = 'FAILED',
                        n.processing_stage = 'failed',
                        n.processing_substage = 'deleted_by_user',
                        n.processing_substage_started_at = datetime(),
                        n.processing_progress = coalesce(n.processing_progress, 100.0),
                        n.processing_completed_at = datetime(),
                        n.processing_owner = null,
                        n.processing_lease_expires_at = null,
                        n.processing_heartbeat_at = datetime(),
                        n.processing_error = 'deleted_by_user',
                        n.processing_llm_active_task = null,
                        n.processing_llm_active_kind = null,
                        n.processing_llm_active_model = null,
                        n.processing_llm_active_endpoint = null
                    RETURN 1 AS touched
                    UNION
                    WITH n
                    WITH n WHERE n:Episodic
                      AND NOT coalesce(n.processing_state, '') IN ['PENDING', 'ENRICHING']
                    DETACH DELETE n
                    RETURN 1 AS touched
                    UNION
                    WITH n
                    WITH n WHERE n:TurnEvidence
                    DETACH DELETE n
                    RETURN 1 AS touched
                }
                RETURN count(touched) AS memory_touched
            }
            WITH repairs, source_keys, assertions_deleted, memory_touched,
                 dependent_views_retired, dependent_views_scrubbed,
                 view_repairs_created, watermarks_reset
            UNWIND CASE WHEN size(source_keys) = 0 THEN [null] ELSE source_keys END AS source_key
            OPTIONAL MATCH (h:TypedAssertionHead {source_key: source_key})
            WHERE source_key IS NOT NULL AND NOT (h)-[:HAS_VERSION]->(:TypedAssertion)
            WITH repairs, assertions_deleted, memory_touched, collect(h) AS orphan_heads,
                 dependent_views_retired, dependent_views_scrubbed,
                 view_repairs_created, watermarks_reset
            FOREACH (h IN orphan_heads | DETACH DELETE h)
            RETURN assertions_deleted, memory_touched, repairs,
                   size([h IN orphan_heads WHERE h IS NOT NULL]) AS heads_deleted,
                   dependent_views_retired, dependent_views_scrubbed,
                   view_repairs_created, watermarks_reset
            """,
            params={
                "node_uuid": node_uuid,
                "operation_id": operation_id,
                "namespace_key": namespace_key,
            },
        )
        if not rows:
            return self._empty_memory_erasure_result()
        row = dict(rows[0]) if rows else {}
        return {
            "touched": bool(int(row.get("memory_touched", 0) or 0)
                            or int(row.get("assertions_deleted", 0) or 0)),
            "memory_touched": int(row.get("memory_touched", 0) or 0),
            "assertions_deleted": int(row.get("assertions_deleted", 0) or 0),
            "heads_deleted": int(row.get("heads_deleted", 0) or 0),
            "dependent_views_retired": int(row.get("dependent_views_retired", 0) or 0),
            "dependent_views_scrubbed": int(row.get("dependent_views_scrubbed", 0) or 0),
            "view_repairs_created": int(row.get("view_repairs_created", 0) or 0),
            "watermarks_reset": int(row.get("watermarks_reset", 0) or 0),
            "repairs": [dict(r) for r in (row.get("repairs") or [])],
        }

    def delete_memory(self, node_uuid: str) -> bool:
        """Compatibility seam that cannot bypass View invalidation or repair journalling."""
        result = self.delete_memory_with_scalar_cascade(
            node_uuid, operation_id=uuid4().hex
        )
        return bool(result["touched"])

    def delete_namespace_with_scalar_cascade(
        self, group_id: str, namespace: str, *, operation_id: str
    ) -> dict[str, Any]:
        """Atomically delete a graph partition plus its namespace-keyed scalar and event logs (G15/G20).

        In addition to the ``group_id`` partition (which already covers the namespace-keyed
        :EventConsolidationWatermark cursor) and the scalar/episode namespace rows, deletes the
        durable event log: every :TypedEventAssertion in the namespace and every
        :TypedEventAssertionHead that HAS_VERSION to an event assertion in the namespace AND to no
        event assertion outside it. A shared head (still HAS_VERSION to a surviving assertion in
        another namespace) is PRESERVED; its deleted CURRENT is repaired by a later idempotent
        write, and event recall reads durable assertions, so a shared head may temporarily carry no
        CURRENT without data loss. Scalar repair receipts and return shape are unchanged.

        :TurnEvidence is deleted HERE, inside this query, rather than by a follow-up call. It holds
        raw user prompts plus ``cwd`` and ``transcript_path``, and it used to be omitted from this
        clause entirely -- so two of the three deletion paths left it behind. The one path that did
        purge it did so as a separate, unjournaled step AFTER this saga had already committed, which
        meant a crash in that window left raw prompts behind with no unresolved erasure row capable
        of resuming them. Folding the label into this MATCH makes its removal atomic with the rest
        of the partition, which is the only way that durability argument holds."""
        namespace_key = normalize_namespace(namespace)
        rows = self.neo4j.execute(
            """
            MERGE (f:EvidenceNamespaceFence {namespace_key: $namespace_key})
            ON CREATE SET f.generation = 0, f.created_at = datetime()
            SET f.lock_nonce = $operation_id,
                f.locked_at = datetime(),
                f.generation = coalesce(f.generation, 0) + 1,
                f.last_reset_operation_id = $operation_id,
                f.last_reset_at = datetime()
            WITH f
            OPTIONAL MATCH (a:TypedAssertion {namespace: $namespace})
            WITH f, collect(DISTINCT CASE WHEN a IS NULL THEN null ELSE {
                     subject_uuid: a.subject_uuid,
                     namespace: a.namespace
                 } END) AS repairs
            FOREACH (repair IN repairs |
                MERGE (rr:ScalarProjectionRepair {
                    repair_key: $operation_id + '\u001fNAMESPACE_DELETE\u001f'
                                + coalesce(repair.namespace, '\u0000null') + '\u001f'
                                + repair.subject_uuid
                })
                ON CREATE SET rr.operation_id = $operation_id,
                              rr.operation_kind = 'NAMESPACE_DELETE',
                              rr.namespace = repair.namespace,
                              rr.subject_uuid = repair.subject_uuid,
                              rr.status = 'pending', rr.started_at = datetime()
            )
            WITH f, repairs
            OPTIONAL MATCH (n)
            WHERE n.group_id = $group_id
               OR (n:Episodic AND n.namespace = $namespace)
               OR (n:TypedAssertion AND n.namespace = $namespace)
               OR (n:TypedAssertionHead AND n.namespace = $namespace)
               OR (n:ScalarConsolidationWatermark AND n.namespace = $namespace)
               OR (n:TurnEvidence AND n.namespace = $namespace)
               OR (n:EventConsolidationWatermark AND n.group_id = $namespace)
               OR (n:TypedEventAssertion AND n.namespace = $namespace)
               OR (n:TypedEventAssertionHead
                   AND EXISTS { MATCH (n)-[:HAS_VERSION]->(ev:TypedEventAssertion)
                                WHERE ev.namespace = $namespace }
                   AND NOT EXISTS { MATCH (n)-[:HAS_VERSION]->(ev2:TypedEventAssertion)
                                    WHERE ev2.namespace <> $namespace })
            WITH f, repairs, collect(DISTINCT n) AS doomed
            WITH f, repairs, doomed,
                 [n IN doomed WHERE coalesce(n.uuid, n.turn_id) IS NOT NULL |
                    coalesce(n.uuid, n.turn_id)] AS doomed_uuids
            OPTIONAL MATCH (v:Entity)
            WHERE (coalesce(v.is_view, false)
                   OR coalesce(v.is_quantstate, false)
                   OR v.view_kind IS NOT NULL)
              AND NOT v IN doomed
              AND (
                  any(eid IN coalesce(v.episode_uuids, []) WHERE eid IN doomed_uuids)
                  OR v.turn_evidence_uuid IN doomed_uuids
                  OR EXISTS {
                      MATCH (evidence)-[:MENTIONS]->(v)
                      WHERE evidence IN doomed
                  }
              )
            WITH f, repairs, doomed, doomed_uuids,
                 collect(DISTINCT v) AS dependent_views
            WITH f, repairs, doomed, doomed_uuids, dependent_views,
                 [v IN dependent_views
                  WHERE coalesce(v.view_current, v.qs_current, true)
                    AND NOT coalesce(v.retired, false)] AS current_views,
                 [v IN dependent_views
                  | coalesce(v.namespace, v.group_id, 'default')] AS dependent_namespaces
            FOREACH (v IN dependent_views |
                SET v.episode_uuids = [eid IN coalesce(v.episode_uuids, [])
                                       WHERE NOT eid IN doomed_uuids],
                    v.supporting_event_count = size([eid IN coalesce(v.episode_uuids, [])
                                                     WHERE NOT eid IN doomed_uuids])
            )
            FOREACH (v IN [candidate IN dependent_views
                           WHERE candidate.turn_evidence_uuid IN doomed_uuids] |
                REMOVE v.turn_evidence_uuid
            )
            FOREACH (v IN current_views |
                SET v.view_current = false,
                    v.qs_current = false,
                    v.retired = true,
                    v.retired_reason = 'contributing_namespace_erased',
                    v.expired_at = datetime(),
                    v.last_accessed = datetime()
                REMOVE v.ss_view_key_current
            )
            WITH f, repairs, doomed, doomed_uuids, dependent_views, current_views,
                 dependent_namespaces,
                 [v IN current_views | {
                     view: v,
                     source_family: CASE
                         WHEN v.view_kind IN ['scalar_state', 'scalar_history']
                         THEN 'typed_scalar_assertions'
                         WHEN v.view_kind = 'timeline' AND EXISTS {
                             MATCH (v)-[:EVENT_HISTORY_ENTRY]->(:TypedEventAssertion)
                         }
                         THEN 'typed_event_assertions'
                         ELSE 'none'
                     END,
                     reconstructible: CASE
                         WHEN v.view_kind IN ['scalar_state', 'scalar_history'] AND EXISTS {
                             MATCH (v)-[:CURRENT_ANCHOR|CONTRIBUTED_TO|HISTORY_ENTRY]
                                   ->(source:TypedAssertion)
                             WHERE NOT source IN doomed
                         }
                         THEN true
                         WHEN v.view_kind = 'timeline' AND EXISTS {
                             MATCH (v)-[:EVENT_HISTORY_ENTRY]->(source:TypedEventAssertion)
                             WHERE NOT source IN doomed
                         }
                         THEN true
                         ELSE false
                     END
                 }] AS view_repairs
            FOREACH (repair IN view_repairs |
                MERGE (rr:ViewProjectionRepair {
                    repair_key: $operation_id + '\u001f' + repair.view.uuid
                })
                ON CREATE SET rr.operation_id = $operation_id,
                              rr.operation_kind = 'NAMESPACE_ERASURE',
                              rr.view_uuid = repair.view.uuid,
                              rr.view_key = coalesce(repair.view.view_key, repair.view.qs_key),
                              rr.view_kind = repair.view.view_kind,
                              rr.namespace = coalesce(
                                  repair.view.namespace, repair.view.group_id, 'default'),
                              rr.namespace_key = f.namespace_key,
                              rr.fence_generation = f.generation,
                              rr.view_subtype = repair.view.view_subtype,
                              rr.subject_uuid = repair.view.view_subject_uuid,
                              rr.predicate = repair.view.view_predicate,
                              rr.domain = coalesce(repair.view.view_domain, ''),
                              rr.source_family = repair.source_family,
                              rr.reconstructible = repair.reconstructible,
                              rr.remaining_evidence_count = size(
                                  coalesce(repair.view.episode_uuids, [])),
                              rr.status = CASE
                                  WHEN repair.reconstructible THEN 'pending'
                                  ELSE 'terminal_not_rebuildable'
                              END,
                              rr.terminal_reason = CASE
                                  WHEN NOT repair.reconstructible
                                  THEN 'not_rebuildable'
                                  ELSE null
                              END,
                              rr.started_at = datetime()
            )
            WITH f, repairs, doomed, dependent_views, current_views,
                 dependent_namespaces
            CALL {
                WITH f, dependent_namespaces
                WITH [f.namespace_key] + dependent_namespaces AS namespaces
                UNWIND namespaces AS ns
                OPTIONAL MATCH (w)
                WHERE (w:ConsolidationWatermark OR w:ScalarConsolidationWatermark
                       OR w:EventConsolidationWatermark)
                  AND (coalesce(w.group_id, w.namespace, '') = ns
                       OR (ns = 'default'
                           AND coalesce(w.group_id, w.namespace, '') = ''))
                WITH collect(DISTINCT w) AS watermarks
                FOREACH (w IN watermarks | DETACH DELETE w)
                RETURN size([w IN watermarks WHERE w IS NOT NULL]) AS watermarks_reset
            }
            FOREACH (n IN doomed | DETACH DELETE n)
            RETURN size(doomed) AS deleted, repairs,
                   size(current_views) AS dependent_views_retired,
                   size(dependent_views) AS dependent_views_scrubbed,
                   size(current_views) AS view_repairs_created,
                   watermarks_reset
            """,
            params={
                "group_id": group_id,
                "namespace": namespace,
                "namespace_key": namespace_key,
                "operation_id": operation_id,
            },
        )
        row = dict(rows[0]) if rows else {}
        return {
            "deleted": int(row.get("deleted", 0) or 0),
            "dependent_views_retired": int(row.get("dependent_views_retired", 0) or 0),
            "dependent_views_scrubbed": int(row.get("dependent_views_scrubbed", 0) or 0),
            "view_repairs_created": int(row.get("view_repairs_created", 0) or 0),
            "watermarks_reset": int(row.get("watermarks_reset", 0) or 0),
            "repairs": [dict(r) for r in (row.get("repairs") or [])],
        }
