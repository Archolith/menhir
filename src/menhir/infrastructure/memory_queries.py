"""Read-path query repository extracted from MemoryGraphAdapter.

Houses memory listing, lookup, flag/delete, and recall-scoring queries.
All methods preserve the exact signatures and return types of the
original MemoryGraphAdapter methods they replace.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Iterator, Sequence
from datetime import timezone
from typing import Any
from uuid import uuid4

from menhir.domain.bootstrap_scope import (
    bootstrap_selection,
    normalize_bootstrap_scope,
    normalize_pin_priority,
)
from menhir.domain.namespace import (
    DEFAULT_NAMESPACE,
    normalize_namespace,
    namespace_spellings,
    namespace_to_group_ids,
    silo_key_cypher,
    tenant_scope_cypher,
    tenant_scope_params,
    tenant_scope_prefilter_cypher,
)
from menhir.domain.recall_visibility import default_recall_visibility_cypher, memory_retired_cypher
from menhir.domain.retention import destructive_retention_allowed_cypher
from menhir.domain.structural_memory import non_structural_memory_cypher
from menhir.domain.episode_nodes import menhir_queue_episode_cypher
from menhir.domain.recall import adjacency_edge_pattern
from menhir.domain.verbatim_skip_status import effective_state_cypher, skip_projection_lookup_cypher
from menhir.infrastructure.vector_indexes import (
    EPISODIC_CONTENT_EMBEDDING,
    TYPED_ASSERTION_NAME_EMBEDDING,
    VectorIndexCatalog,
    VectorIndexSpec,
    ann_call,
    index_backed_rows_sync,
)
from menhir.infrastructure.cypher import (
    Cypher,
    ENTITY_METADATA_FIELDS,
    EPISODE_NOT_ERASED,
    EPISODE_NOT_MARKED,
    FACT_TEMPORAL_FIELDS,
    FACT_NOT_REDERIVE_PENDING,
    FACT_TEXT_PRESENT,
    MEMORY_RETURN_FIELDS,
    memory_recency_cypher,
    non_derived_view_cypher,
    SHADOW_CANDIDATE_FACT_EDGE_FIELDS,
)
from menhir.infrastructure.anchored_time_persist import PRESERVED_EDGE_ATTRIBUTES
from menhir.infrastructure.erasure_fence import marker_absent_cypher
from menhir.infrastructure.graph_text_inventory import (
    ABSORBED_BLOCK_SEPARATOR,
    ENTITY_STRUCTURAL_PROPERTIES,
)
from menhir.infrastructure.consolidation_queries import automatic_lifecycle_protection_cypher
from menhir.infrastructure.neo4j import Neo4jRepository
from menhir.infrastructure.verifier_repository import candidate_verifier_evidence_cypher
from menhir.services.embedding_identity import LEGACY_EMBEDDING_MODELS

logger = logging.getLogger(__name__)

#: A conflict pending LLM review longer than this counts as stuck (the review runs hourly).
#: Matches the queue-health warning threshold in ``scheduler_tasks``.
STALE_PENDING_CONFLICT_SECONDS = 24 * 3600
STALE_PENDING_CONFLICT_DURATION = f"PT{STALE_PENDING_CONFLICT_SECONDS}S"

#: CF-332: the absorbed-block header written by conflict resolution, as a Cypher literal.
ABSORBED_BLOCK_SEPARATOR_CYPHER = json.dumps(ABSORBED_BLOCK_SEPARATOR)


def _absorbed_block_cut(expr: str) -> str:
    """Cypher: ``expr`` with every absorbed block whose source is in ``cut_uuids`` removed (null
    stays null)."""
    sep = ABSORBED_BLOCK_SEPARATOR_CYPHER
    return (
        f"CASE WHEN {expr} IS NULL THEN null ELSE reduce(acc = head(split({expr}, {sep})), "
        f"p IN tail(split({expr}, {sep})) | CASE WHEN any(u IN cut_uuids "
        f"WHERE p STARTS WITH u + ')') THEN acc ELSE acc + {sep} + p END) END"
    )

#: Turn capture is running and the admission join has NEVER been drawn (CF-229).
ADMISSION_NEVER_LINKED = "never_linked"
#: Turns captured and at least one join drawn -- the wiring works.
ADMISSION_LINKED = "linked"
#: No turns captured, so there is nothing to pair and nothing to report.
ADMISSION_NO_TURNS = "no_turns"

_OPAQUE_DIGEST_PATTERN = re.compile(r"^[A-Za-z0-9_-]{32,256}$")
_DIGEST_KEY_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")

# Single source of truth for what a namespace erase removes directly (over variable `n`, params
# $group_id and $namespace). Delete, count and uuid capture all use it so they cannot drift.
NAMESPACE_ERASE_PREDICATE = """n.group_id = $group_id
               OR (n:Episodic AND n.namespace = $namespace)
               OR (n:TypedAssertion AND n.namespace = $namespace)
               OR (n:TypedAssertionHead AND n.namespace = $namespace)
               OR (n:ScalarConsolidationWatermark AND n.namespace = $namespace)
               OR (n:TurnEvidence AND n.namespace = $namespace)
               OR (n:EventConsolidationWatermark AND n.group_id = $namespace)
               OR (n:TypedEventAssertion AND n.namespace = $namespace)
               OR (n:TypedEventAssertionHead AND EXISTS { MATCH (n)-[:HAS_VERSION]->(ev:TypedEventAssertion) WHERE ev.namespace = $namespace }
                   AND NOT EXISTS { MATCH (n)-[:HAS_VERSION]->(ev2:TypedEventAssertion)
                                    WHERE ev2.namespace <> $namespace })
               OR (n:Todo AND n.namespace = $namespace)
               OR (n:WorkArtifact AND n.namespace = $namespace)"""

# A node's silo key over `n`: the rule erase_target_namespace_keys applies to an erase target.
_SILO_KEY_CYPHER = silo_key_cypher("n")

# list_namespace_records: per kind, the label, the id an erase accepts (erase_target_namespace_keys),
# its time, a preview, and the filters that keep non-user rows out.
_LISTABLE_KINDS: dict[str, dict[str, Any]] = {
    "memory": {
        "label": "Episodic",
        "id": "n.uuid",
        "time": "n.created_at",
        "preview": "n.content",
        "source": "n.source",
        "state": "n.processing_state",
        "where": (
            non_structural_memory_cypher("n"),
            EPISODE_NOT_ERASED,
            # A Graphiti twin is erased with its queue node; listing it would erase twice.
            "NOT EXISTS { MATCH (q:Episodic) WHERE q.resolved_episode_uuid = n.uuid }",
        ),
    },
    "turn_evidence": {
        "label": "TurnEvidence",
        "id": "n.turn_id",
        "time": "n.recorded_at",
        "preview": "n.text",
        "source": "n.role",
        "state": "null",
        "where": (),
    },
    "typed_assertion": {
        "label": "TypedAssertion",
        "id": "n.assertion_id",
        "time": "n.recorded_at",
        "preview": "coalesce(n.subject_display, '') + ' ' + coalesce(n.attribute, '') + ' = '"
                   " + coalesce(toString(n.value), '')",
        "source": "n.episode_uuid",
        "state": "null",
        "where": (),
    },
}

# Subordinate expansion. Input: `direct` (list of directly doomed nodes). Output column: `subs`,
# the Todo/WorkArtifact children that carry no namespace/group_id themselves and have no
# incoming owner edge from a node outside `direct`. Use as: WITH ..., direct <this> then
# WITH ..., direct + subs AS doomed.
NAMESPACE_SUBORDINATES_CALL = """
            CALL {
                WITH direct
                UNWIND direct AS d
                OPTIONAL MATCH (d)-[:HAS_LOCATION|EMBODIED_IN|HAS_OPEN_QUESTION|DECLARES]->(s)
                WHERE (
                        (d:Todo AND s:TodoLocation)
                        OR (d:WorkArtifact AND (s:ArtifactSource OR s:ArtifactLocation
                                                OR s:OpenQuestion OR s:ArtifactDeclaration))
                      )
                  AND NOT s IN direct
                  AND NOT EXISTS {
                      MATCH (o)-[:HAS_LOCATION|EMBODIED_IN|HAS_OPEN_QUESTION|DECLARES]->(s)
                      WHERE NOT o IN direct
                  }
                RETURN collect(DISTINCT s) AS subs
            }
"""

# Primary label per node for by_label reporting: first match in this order, else head(labels).
NAMESPACE_PRIMARY_LABEL_CASE = """CASE
                WHEN n:Todo THEN 'Todo'
                WHEN n:WorkArtifact THEN 'WorkArtifact'
                WHEN n:TodoLocation THEN 'TodoLocation'
                WHEN n:ArtifactSource THEN 'ArtifactSource'
                WHEN n:ArtifactLocation THEN 'ArtifactLocation'
                WHEN n:OpenQuestion THEN 'OpenQuestion'
                WHEN n:ArtifactDeclaration THEN 'ArtifactDeclaration'
                WHEN n:TurnEvidence THEN 'TurnEvidence'
                WHEN n:Episodic THEN 'Episodic'
                WHEN n:Entity THEN 'Entity'
                WHEN n:TypedAssertionHead THEN 'TypedAssertionHead'
                WHEN n:TypedAssertion THEN 'TypedAssertion'
                WHEN n:TypedEventAssertionHead THEN 'TypedEventAssertionHead'
                WHEN n:TypedEventAssertion THEN 'TypedEventAssertion'
                WHEN n:ScalarConsolidationWatermark THEN 'ScalarConsolidationWatermark'
                WHEN n:EventConsolidationWatermark THEN 'EventConsolidationWatermark'
                ELSE coalesce(head(labels(n)), 'unlabeled')
            END"""


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


def _iso_utc_or_none(value: Any) -> str | None:
    """Serialise a Neo4j DateTime / datetime / ISO string as an ISO-8601 UTC string."""

    if value is None:
        return None
    if hasattr(value, "to_native"):
        value = value.to_native()
    if isinstance(value, str):
        from menhir.infrastructure.telemetry.operator_warnings_store import parse_iso_utc

        value = parse_iso_utc(value)
        if value is None:
            return None
    if getattr(value, "tzinfo", None) is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _flagged_pin_filter(
    workspace: str | None, namespace: str | None
) -> tuple[list[str], dict[str, Any]]:
    """WHERE clauses and params selecting a bootstrap read's startup pins.

    Shared by the pin list, the receipt version and the per-scope counts so the
    reported totals always describe the same set as the returned pins.
    """

    _selection_key, allowed_scopes = bootstrap_selection(workspace)
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
    return where, params


class MemoryQueryRepository:
    """Encapsulates memory read/query operations and simple mutations (flag, delete)."""

    def __init__(self, neo4j: Neo4jRepository, *, vector_catalog: VectorIndexCatalog | None = None) -> None:
        self.neo4j = neo4j
        #: #574: with a catalog, the embedding lanes use an ONLINE vector index; without, they scan.
        self._vector_catalog = vector_catalog

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

        # Menhir's queue anchors carry `namespace` but no `group_id`, so the scope is the shared
        # tenancy predicate (namespace over group_id), not a bare group_id match.
        params: dict[str, Any] = {}
        node_filter = ""
        if namespace_spellings(namespace) is not None:
            node_filter = f" AND {tenant_scope_cypher('n')}"
            params.update(tenant_scope_params(namespace))

        rows = self.neo4j.execute(
            f"""
            MATCH (n)
            WHERE (n:Entity OR n:Episodic){node_filter}
            {skip_projection_lookup_cypher("n", "p")}
            WITH n, {effective_state_cypher("n", "p")} AS effective_state,
                 CASE WHEN n:Entity AND n.conflict_group_id IS NOT NULL
                      THEN {automatic_lifecycle_protection_cypher("n")}
                      ELSE false END AS conflict_live,
                 coalesce(n.conflict_pending_since, n.conflict_created_at) AS pending_since
            RETURN count(n) AS total_memories,
                   count(CASE WHEN n:Entity THEN 1 END) AS entity_count,
                   count(CASE WHEN n:Episodic THEN 1 END) AS episode_count,
                   count(CASE WHEN coalesce(n.user_flagged, false) THEN 1 END) AS flagged_count,
                   count(CASE WHEN n.scope = 'SESSION' THEN 1 END) AS session_count,
                   count(CASE WHEN n.scope = 'PERSISTENT' THEN 1 END) AS persistent_count,
                   count(CASE WHEN n.scope = 'PROMOTED' THEN 1 END) AS promoted_count,
                   count(CASE WHEN n:Episodic AND effective_state = 'PENDING' THEN 1 END) AS pending_count,
                   count(CASE WHEN n:Episodic AND effective_state = 'ENRICHING' THEN 1 END) AS enriching_count,
                   count(CASE WHEN n:Episodic AND effective_state = 'READY' THEN 1 END) AS ready_count,
                   count(CASE WHEN n:Episodic AND effective_state = 'FAILED' THEN 1 END) AS failed_count,
                   max(CASE WHEN n:Episodic AND effective_state = 'FAILED'
                            THEN n.processing_completed_at END) AS latest_failed_at,
                   count(DISTINCT CASE WHEN conflict_live
                                        AND n.conflict_status = 'pending_llm_review'
                                       THEN n.conflict_group_id END) AS pending_conflict_groups,
                   min(CASE WHEN conflict_live
                                 AND n.conflict_status = 'pending_llm_review'
                            THEN pending_since END) AS oldest_pending_conflict_at,
                   count(DISTINCT CASE WHEN conflict_live
                                        AND n.conflict_status = 'unresolved'
                                       THEN n.conflict_group_id END) AS unresolved_conflict_groups,
                   count(DISTINCT CASE WHEN conflict_live
                                        AND n.conflict_status = 'pending_llm_review'
                                        AND pending_since < datetime() - duration('{STALE_PENDING_CONFLICT_DURATION}')
                                       THEN n.conflict_group_id END) AS stale_pending_conflict_groups,
                   // When a group began needing attention: a member's confirmation (or detection,
                   // for a PROMOTED pair or a pre-stamp graph), or a pending member going stale.
                   // A new conflict needing attention moves this past an acknowledgement.
                   max(CASE
                         WHEN conflict_live AND n.conflict_status = 'unresolved'
                         THEN CASE WHEN n.conflict_confirmed_at IS NOT NULL
                                        AND (n.conflict_created_at IS NULL
                                             OR n.conflict_confirmed_at > n.conflict_created_at)
                                   THEN n.conflict_confirmed_at ELSE n.conflict_created_at END
                         WHEN conflict_live AND n.conflict_status = 'pending_llm_review'
                              AND pending_since < datetime() - duration('{STALE_PENDING_CONFLICT_DURATION}')
                         THEN pending_since + duration('{STALE_PENDING_CONFLICT_DURATION}')
                       END) AS latest_conflict_attention_at
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
            "latest_failed_at": None,
            "pending_conflict_groups": 0,
            "oldest_pending_conflict_at": None,
            "unresolved_conflict_groups": 0,
            "stale_pending_conflict_groups": 0,
            "latest_conflict_attention_at": None,
        }
        overview["latest_failed_at"] = _iso_utc_or_none(overview.get("latest_failed_at"))
        overview["pending_conflict_groups"] = int(overview.get("pending_conflict_groups") or 0)
        overview["oldest_pending_conflict_at"] = _iso_utc_or_none(
            overview.get("oldest_pending_conflict_at")
        )
        overview["unresolved_conflict_groups"] = int(overview.get("unresolved_conflict_groups") or 0)
        overview["stale_pending_conflict_groups"] = int(
            overview.get("stale_pending_conflict_groups") or 0
        )
        overview["latest_conflict_attention_at"] = _iso_utc_or_none(
            overview.get("latest_conflict_attention_at")
        )
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
            # Legacy raw-capture copies duplicate a source episode's text (#662).
            "NOT coalesce(n['raw_capture'], false)",
        ]
        params: dict[str, Any] = {"limit": safe_limit}
        if namespace is not None and str(namespace).strip():
            where.append("coalesce(n.namespace, 'default') = $namespace")
            params["namespace"] = str(namespace).strip()
        query = (Cypher()
            .match("(n)")
            .where(*where)
            .with_clause(f"n, {memory_recency_cypher()} AS recency")
            .return_fields(MEMORY_RETURN_FIELDS)
            .order_by("recency IS NOT NULL DESC, recency DESC, n.uuid")
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
        where, params = _flagged_pin_filter(workspace, namespace)
        params["limit"] = safe_limit
        query = (Cypher()
            .match("(n)")
            .where(*where)
            .with_clause(f"n, {memory_recency_cypher()} AS recency")
            .return_fields(MEMORY_RETURN_FIELDS, "n.pin_priority AS pin_priority")
            # #674: explicit priority first, then the workspace's own pins ahead of
            # general ones, then recency, so a limit drops the least important pins.
            .order_by(
                "coalesce(n.pin_priority, 0) DESC, "
                "CASE WHEN n.bootstrap_scope STARTS WITH 'workspace:' THEN 0 ELSE 1 END, "
                "recency IS NOT NULL DESC, recency DESC, n.uuid"
            )
            .limit()
            .build())
        return self.neo4j.execute(query, params=params)

    def fetch_flagged_pin_counts(
        self,
        workspace: str | None = None,
        *,
        namespace: str | None = None,
    ) -> dict[str, int]:
        """Count the startup pins a bootstrap read selects, keyed by bootstrap scope."""

        where, params = _flagged_pin_filter(workspace, namespace)
        rows = self.neo4j.execute(
            f"""
            MATCH (n)
            WHERE {" AND ".join(where)}
            RETURN n.bootstrap_scope AS bootstrap_scope, count(n) AS pins
            """,
            params=params,
        )
        return {
            str(row["bootstrap_scope"]): int(row.get("pins") or 0)
            for row in rows
            if row.get("bootstrap_scope")
        }

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

        selection_key, _allowed_scopes = bootstrap_selection(workspace)
        where, params = _flagged_pin_filter(workspace, namespace)
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
            # '' is the same silo as 'default' (namespace_spellings); without the second clause
            # an ownership check for 'default' refuses a node stored as '' as foreign.
            where.append(
                "(coalesce(n.namespace, 'default') = $namespace"
                " OR ($namespace = 'default' AND n.namespace = ''))"
            )
            params["namespace"] = str(namespace).strip()
        query = (Cypher()
            .match("(n)")
            .where(*where)
            .return_fields(MEMORY_RETURN_FIELDS)
            .limit("1")
            .build())
        rows = self.neo4j.execute(query, params=params)
        return rows[0] if rows else None

    def list_namespace_records(
        self,
        namespace: str,
        *,
        kind: str = "memory",
        limit: int = 50,
        after_id: str | None = None,
        source: str | None = None,
    ) -> list[dict[str, Any]]:
        """One page of a silo's erasable records of one kind, ordered by the id the erase takes.

        Silo membership uses the same key as ``erase_target_namespace_keys`` (namespace, else
        group_id, blank -> 'default'), so what is listed is what the erase's scope check accepts.
        Never matches by group_id alone and never lists :Entity, structure rows, evidence
        projections or Graphiti twins (each twin is erased with its queue node).
        """
        spec = _LISTABLE_KINDS.get(kind)
        if spec is None:
            raise ValueError(f"unknown kind {kind!r}; expected one of {sorted(_LISTABLE_KINDS)}")
        where = [
            f"{_SILO_KEY_CYPHER} = $ns_key",
            f"{spec['id']} IS NOT NULL",
            f"($after_id = '' OR {spec['id']} > $after_id)",
            *spec["where"],
        ]
        params: dict[str, Any] = {
            "ns_key": normalize_namespace(namespace),
            "after_id": (after_id or "").strip(),
            "limit": max(1, min(int(limit), 100)),
        }
        if source and kind == "memory":
            where.append("n.source = $source")
            params["source"] = source
        query = (
            f"MATCH (n:{spec['label']}) WHERE " + " AND ".join(where)
            + f" RETURN {spec['id']} AS id, toString({spec['time']}) AS recorded_at,"
            f" left(toString(coalesce({spec['preview']}, '')), 200) AS preview,"
            f" {spec['source']} AS source, {spec['state']} AS processing_state"
            " ORDER BY id LIMIT $limit"
        )
        return [
            {**row, "kind": kind} for row in self.neo4j.execute(query, params=params)
        ]

    #: Labels that hold a namespace's data, each with an index on `namespace` (schema.py).
    _NAMESPACE_LABELS = ("Episodic", "Entity", "Todo", "WorkArtifact", "TurnEvidence")

    def namespace_status(self, namespace: str, *, include_similar: bool) -> dict[str, Any]:
        """Whether any record carries exactly *namespace*, and stored spellings it may mean (#671).

        Exact match on purpose: reads match the namespace verbatim, so `PFCC` or `pfcc ` finds
        nothing when the data is under `pfcc`. ``similar`` lists the stored namespaces equal to it
        after stripping and case-folding; it is for unpinned callers only.
        """
        if namespace in ("", DEFAULT_NAMESPACE):
            return {"namespace": namespace, "known": True, "similar": []}
        # The same silo predicate reads use (CF-127), so "known" means "a read would find it".
        scope = tenant_scope_cypher("n")
        known = any(
            self.neo4j.execute(
                f"MATCH (n:{label}) WHERE {scope} RETURN 1 AS hit LIMIT 1",
                params=tenant_scope_params(namespace),
            )
            for label in self._NAMESPACE_LABELS
        )
        similar: list[str] = []
        if not known and include_similar:
            key = namespace.strip().casefold()
            values: set[str] = {DEFAULT_NAMESPACE}
            for label in self._NAMESPACE_LABELS:
                rows = self.neo4j.execute(
                    f"MATCH (n:{label}) WITH coalesce(n.namespace, n.group_id) AS v "
                    "WHERE v IS NOT NULL RETURN DISTINCT v LIMIT 1000",
                    params={},
                )
                values.update(str(r["v"]) for r in rows if r.get("v") is not None)
            similar = sorted(v for v in values if v != namespace and v.strip().casefold() == key)
        return {"namespace": namespace, "known": known, "similar": similar}

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
            .with_clause(f"n, {memory_recency_cypher()} AS recency")
            .return_fields(MEMORY_RETURN_FIELDS)
            .order_by("recency IS NOT NULL DESC, recency DESC, n.uuid")
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
            .with_clause(f"n, {memory_recency_cypher()} AS recency")
            .return_fields(MEMORY_RETURN_FIELDS)
            .order_by("recency IS NOT NULL DESC, recency DESC, n.uuid")
            .limit()
            .build())
        return self.neo4j.execute(query, params=params)

    # --- Recall scoring helpers ---------------------------------------------

    def search_episode_embeddings(
        self,
        query_vector: list[float],
        *,
        limit: int = 10,
        namespace: str | None = None,
        model: str | None = None,
        subject_uuid: str | None = None,
        resolved_only: bool = False,
        sources: Sequence[str] | None = None,
        include_retired: bool = False,
    ) -> list[dict[str, object]]:
        """Cosine hits over Menhir `:Episodic` content embeddings (source-memory lane).

        ``include_retired`` admits memories carrying the ``memory_retired`` marker (historical
        lookups: recall_history, recall_timeline); the default source lane hides them. Each row
        carries ``memory_retired`` so historical callers can label it.

        With ``resolved_only``, only receipts whose resolved Graphiti episode can anchor a timeline
        are searched (store-only receipts and hidden/FAILED/foreign episodes have no position).

        With ``model``, only vectors stamped by that embedder are compared: a different model of
        the same dimension lives in a different space (audit F2). With ``subject_uuid``, only
        receipts whose resolved episode mentions that entity are searched, so the constraint
        applies before ``LIMIT`` (recall_timeline query + subject).

        With ``sources``, only episodes whose ingest ``source`` is one of those exact strings
        match, before the limit is applied. An empty sequence matches nothing; ``None`` is no filter.

        Read-only; pairs with the ingest-side `set_episode_content_embedding` write.
        Uses the same shared structural/visibility predicates and the shared
        `tenant_scope_cypher` tenancy predicate as the rest of the domain ('' and 'default' are
        the same silo; None is unscoped), so only memories generic recall could surface are
        searched. Only
        Menhir's own queue nodes match (`menhir_queue_episode_cypher`; `processing_state` alone
        does not identify them -- the schema migration stamps it on Graphiti's episodes too, #215).
        A FAILED enrichment stays searchable (#662); erase tombstones and episodes with an erase
        marker (sealed, delete not yet applied) are excluded. A
        stale-dimension embedding is skipped by the size guard instead of erroring
        the query. Evidence projections are excluded by
        `non_structural_memory_cypher`.
        """
        safe_limit = max(1, min(limit, 50))
        where = [
            "n.content_embedding IS NOT NULL",
            "size(n.content_embedding) = size($query_vector)",
            menhir_queue_episode_cypher("n"),
            "n.processing_state IS NOT NULL",
            # A FAILED enrichment keeps its text recallable here (#662); an erase tombstone
            # (also FAILED, substage deleted_by_user) is not, nor is an episode whose erase is
            # sealed but not yet applied.
            EPISODE_NOT_ERASED,
            EPISODE_NOT_MARKED,
            non_structural_memory_cypher("n"),
            default_recall_visibility_cypher("n", include_retired=include_retired),
        ]
        # The twin lookup labels history rows only; the default lane hides retired rows already and
        # must read memory nodes through the vector index alone (#574 index-only plan).
        retired_label = (
            "(coalesce(n.memory_retired, false) OR EXISTS { MATCH (rt:Episodic) "
            "WHERE rt.uuid = n.resolved_episode_uuid AND coalesce(rt.memory_retired, false) })"
            if include_retired else "coalesce(n.memory_retired, false)"
        )
        params: dict[str, Any] = {
            "query_vector": query_vector,
            "limit": safe_limit,
            **tenant_scope_params(namespace),
        }
        if model is not None:
            where.append(
                "(coalesce(n.content_embedding_model, '') = $embedding_model"
                " OR coalesce(n.content_embedding_model, '') IN $legacy_embedding_models)"
            )
            params["embedding_model"] = model
            params["legacy_embedding_models"] = list(LEGACY_EMBEDDING_MODELS)
        if sources is not None:
            where.append("n.source IN $sources")
            params["sources"] = list(sources)
        if resolved_only or subject_uuid is not None:
            # The resolved episode must itself be one `timeline_anchor` could return (same
            # shared predicates), so hidden/FAILED/foreign episodes do not use up LIMIT slots.
            # A FAILED receipt never anchors either, even with a resolved episode (#662).
            where.append("n.processing_state <> 'FAILED'")
            episode_pattern = "(g:Episodic)"
            if subject_uuid is not None:
                episode_pattern = "(:Entity {uuid:$subject_uuid})<-[:MENTIONS]-(g:Episodic)"
                params["subject_uuid"] = subject_uuid
            # Same prefilter as `_timeline_episode_where`, so a hit here is one it can anchor.
            prefilter = (
                [tenant_scope_prefilter_cypher("g")]
                if namespace_spellings(namespace) is not None else []
            )
            where.append(
                f"EXISTS {{ MATCH {episode_pattern}"
                " WHERE g.uuid = n.resolved_episode_uuid AND "
                + " AND ".join([
                    *prefilter,
                    "g.valid_at IS NOT NULL",
                    "coalesce(g.processing_state, '') <> 'FAILED'",
                    non_structural_memory_cypher("g"),
                    default_recall_visibility_cypher("g", include_retired=include_retired),
                    tenant_scope_cypher("g"),
                ])
                + " }"
            )
        # menhir #171: when scoped, lead with the index-friendly prefilter so the planner can
        # seek the :Episodic(namespace)/:Episodic(group_id) RANGE indexes. It is a superset;
        # tenant_scope_cypher below still decides membership. Unscoped (None) is unchanged.
        if namespace_spellings(namespace) is not None:
            where.insert(0, tenant_scope_prefilter_cypher("n"))
        where.append(tenant_scope_cypher("n"))
        tail = f"""
            WITH n, vector.similarity.cosine(n.content_embedding, $query_vector) AS cosine
            WHERE cosine IS NOT NULL
            RETURN n.uuid AS uuid, n.content AS content, n.source AS source,
                   n.session_id AS session_id,
                   toString(coalesce(n.reference_time, n.created_at)) AS reference_time,
                   {retired_label} AS memory_retired,
                   cosine
            ORDER BY cosine DESC, n.uuid
            LIMIT $limit
            """
        # #574: a subject constraint pins a few receipts; the scan is exact and cheap there.
        if subject_uuid is None:
            rows = self._index_backed(
                EPISODIC_CONTENT_EMBEDDING, "n", where, tail, params, query_vector, safe_limit
            )
            if rows is not None:
                return rows
        return self.neo4j.execute(
            f"""
            MATCH (n:Episodic)
            WHERE {" AND ".join(where)}
            """ + tail,
            params=params,
        )

    def _index_backed(
        self,
        spec: VectorIndexSpec,
        var: str,
        where: list[str],
        tail: str,
        params: dict[str, Any],
        query_vector: list[float],
        limit: int,
    ) -> list[dict[str, Any]] | None:
        """Run ``WHERE where`` + ``tail`` over vector-index candidates only, in place of the
        caller's ``MATCH`` scan (the index covers exactly that label and property).

        None means scan: no catalog, no usable index, or the candidates could not be shown complete."""
        if self._vector_catalog is None:
            return None
        hydrate_query = f"{ann_call(spec, var)} WITH {var} WHERE {' AND '.join(where)} " + tail

        def hydrate(index_name: str, k: int) -> list[dict[str, Any]]:
            return self.neo4j.execute(hydrate_query, params={**params, "index_name": index_name, "k": k})

        return index_backed_rows_sync(
            execute=lambda query, query_params: self.neo4j.execute(query, params=query_params),
            catalog=self._vector_catalog,
            spec=spec,
            vector=query_vector,
            limit=limit,
            hydrate=hydrate,
            score_key="cosine",
        )

    # --- Timeline navigation (recall_timeline, read-only) --------------------

    def _timeline_episode_where(
        self, *, namespace: str | None, params: dict[str, Any]
    ) -> list[str]:
        """The exact visible-episode predicates of `search_episode_embeddings`, minus
        the embedding ones, plus the `valid_at` requirement of the total order (A1/A2).

        Shared by `timeline_page` and `timeline_anchor` so the two can never drift.
        Tenancy goes through the shared helpers only: `tenant_scope_prefilter_cypher`
        FIRST when the namespace is scoped (same `namespace_spellings` check as #171),
        `tenant_scope_cypher` always. No namespace/group_id comparison is spelled here.
        """
        # Timeline reads Graphiti's resolved :Episodic nodes (they carry valid_at, MENTIONS and the
        # fact edges' `episodes` ids); Menhir's queue node carries reference_time, not valid_at, so
        # `valid_at IS NOT NULL` selects one node per memory. processing_state is only guaranteed on
        # the queue node, so it is NOT required here; explicit FAILED is still excluded.
        where = [
            "n.valid_at IS NOT NULL",
            "coalesce(n.processing_state, '') <> 'FAILED'",
            non_structural_memory_cypher("n"),
            # Timelines are historical: retired memories stay, labelled (memory_retired).
            default_recall_visibility_cypher("n", include_retired=True),
            # The memory still exists: a visible Menhir queue node resolves to this episode. Deleting
            # or erasing a memory by its receipt (queue-node) uuid removes that twin, so the timeline
            # stops showing the raw text even though Graphiti's episode survives (audit Q1 / #119).
            # `episodic_resolved_episode_uuid_idx` backs the lookup. Evidence projections carry their
            # flag on the queue node only, so the twin must also be non-structural (ADR 0001).
            "EXISTS { MATCH (q:Episodic) WHERE q.resolved_episode_uuid = n.uuid AND "
            + default_recall_visibility_cypher("q", include_retired=True) + " AND " + non_structural_memory_cypher("q") + " }",
        ]
        params.update(tenant_scope_params(namespace))
        if namespace_spellings(namespace) is not None:
            where.insert(0, tenant_scope_prefilter_cypher("n"))
        where.append(tenant_scope_cypher("n"))
        return where

    @staticmethod
    def _timeline_match(subject_uuid: str | None) -> str:
        """MENTIONS join when a subject thread is requested, plain episodes otherwise (A3)."""
        if subject_uuid:
            return "(s:Entity {uuid:$subject_uuid})<-[:MENTIONS]-(n:Episodic)"
        return "(n:Episodic)"

    _TIMELINE_RETURN = (
        "n.uuid AS uuid, toString(n.valid_at) AS valid_at, "
        "toString(n.created_at) AS created_at, n.content AS content, "
        "n.session_id AS session_id, n.source AS source, "
        "(coalesce(n.memory_retired, false) OR EXISTS { MATCH (rq:Episodic) "
        "WHERE rq.resolved_episode_uuid = n.uuid AND coalesce(rq.memory_retired, false) }) "
        "AS memory_retired"
    )

    def timeline_page(
        self,
        *,
        namespace: str | None,
        subject_uuid: str | None = None,
        after: tuple[str, str | None, str] | None = None,
        before: tuple[str, str | None, str] | None = None,
        window_from: str | None = None,
        window_to: str | None = None,
        limit: int,
    ) -> list[dict[str, Any]]:
        """One page of the recorded-time total order over visible episodes (A1/A3).

        Order key is `(n.valid_at, coalesce(n.created_at, epoch), n.uuid)`; only
        episodes with a non-null `valid_at` take part. `after`/`before` are exclusive
        cursor keys `(valid_at_iso, created_at_iso, uuid)`; `before` queries DESC with
        LIMIT and the returned rows are reversed so the caller always receives ASC.
        Read-only: never touches `last_accessed`.
        """
        safe_limit = max(1, min(int(limit), 50))
        params: dict[str, Any] = {
            "limit": safe_limit,
            "epoch": "1970-01-01T00:00:00Z",
            "subject_uuid": subject_uuid,
        }
        where = self._timeline_episode_where(namespace=namespace, params=params)
        if after is not None:
            where.append(
                "(n.valid_at > datetime($after_valid_at) "
                "OR (n.valid_at = datetime($after_valid_at) AND "
                "(coalesce(n.created_at, datetime($epoch)) > "
                "coalesce(datetime($after_created_at), datetime($epoch)) OR "
                "(coalesce(n.created_at, datetime($epoch)) = "
                "coalesce(datetime($after_created_at), datetime($epoch)) AND "
                "n.uuid > $after_uuid))))"
            )
            params["after_valid_at"] = after[0]
            params["after_created_at"] = after[1]
            params["after_uuid"] = after[2]
        if before is not None:
            where.append(
                "(n.valid_at < datetime($before_valid_at) "
                "OR (n.valid_at = datetime($before_valid_at) AND "
                "(coalesce(n.created_at, datetime($epoch)) < "
                "coalesce(datetime($before_created_at), datetime($epoch)) OR "
                "(coalesce(n.created_at, datetime($epoch)) = "
                "coalesce(datetime($before_created_at), datetime($epoch)) AND "
                "n.uuid < $before_uuid))))"
            )
            params["before_valid_at"] = before[0]
            params["before_created_at"] = before[1]
            params["before_uuid"] = before[2]
        if window_from is not None:
            where.append("n.valid_at >= datetime($window_from)")
            params["window_from"] = window_from
        if window_to is not None:
            where.append("n.valid_at <= datetime($window_to)")
            params["window_to"] = window_to
        desc = before is not None
        direction = "DESC" if desc else "ASC"
        query = f"""
            MATCH {self._timeline_match(subject_uuid)}
            WHERE {" AND ".join(where)}
            RETURN {self._TIMELINE_RETURN}
            ORDER BY n.valid_at {direction},
                     coalesce(n.created_at, datetime($epoch)) {direction},
                     n.uuid {direction}
            LIMIT $limit
        """
        rows = self.neo4j.execute(query, params=params)
        if desc:
            rows.reverse()
        return rows

    def timeline_anchor(
        self,
        *,
        uuid: str,
        namespace: str | None,
        subject_uuid: str | None = None,
    ) -> dict[str, Any] | None:
        """The anchor episode for `around` navigation, only if it is visible (A3).

        `uuid` may be the Graphiti episode uuid or the memory's receipt uuid (Menhir's
        queue node, which is what add_memory, recall_history and the source-memory
        search return). A receipt uuid resolves through `resolved_episode_uuid` only
        when that queue node is itself visible, non-FAILED and in the caller's silo.

        Applies exactly the A2 predicates (and the MENTIONS join when a subject thread
        is requested), so a hidden or foreign episode is indistinguishable from a
        nonexistent one. Read-only.
        """
        params: dict[str, Any] = {
            "anchor_uuid": uuid,
            "epoch": "1970-01-01T00:00:00Z",
            "subject_uuid": subject_uuid,
        }
        where = self._timeline_episode_where(namespace=namespace, params=params)
        # Receipt uuid -> Graphiti episode uuid, resolved in-query so both kinds share
        # one visibility/tenancy check on the anchor itself.
        resolve = " AND ".join([
            "a.uuid = $anchor_uuid",
            menhir_queue_episode_cypher("a"),
            "coalesce(a.processing_state, '') <> 'FAILED'",
            default_recall_visibility_cypher("a", include_retired=True),
            tenant_scope_cypher("a"),
        ])
        where.append("n.uuid = target_uuid")
        query = f"""
            OPTIONAL MATCH (a:Episodic) WHERE {resolve}
            WITH coalesce(a.resolved_episode_uuid, $anchor_uuid) AS target_uuid
            LIMIT 1
            MATCH {self._timeline_match(subject_uuid)}
            WHERE {" AND ".join(where)}
            RETURN {self._TIMELINE_RETURN}
            LIMIT 1
        """
        rows = self.neo4j.execute(query, params=params)
        return rows[0] if rows else None

    def timeline_facts(
        self, *, episode_uuids: list[str], namespace: str | None
    ) -> dict[str, list[dict[str, Any]]]:
        """RELATES_TO facts attached to the page's episodes, grouped per episode (A3).

        Tenancy on the fact edge goes through `tenant_scope_cypher("r")` only. Bounded:
        at most 50 episodes (clamped here) and 20 facts per episode, ordered by
        `r.valid_at`. Read-only.
        """
        safe_uuids = [u for u in episode_uuids if u][:50]
        if not safe_uuids:
            return {}
        query = f"""
            UNWIND $episode_uuids AS episode_uuid
            MATCH (ep:Episodic {{uuid: episode_uuid}})-[:MENTIONS]->(:Entity)
                  -[r:RELATES_TO]-(:Entity)
            WHERE episode_uuid IN coalesce(r.episodes, [])
              AND {FACT_NOT_REDERIVE_PENDING}
              AND {tenant_scope_cypher("r")}
            WITH DISTINCT episode_uuid, r
            ORDER BY r.valid_at
            WITH episode_uuid, collect({{
                fact: r.fact,
                valid_at: toString(r.valid_at),
                invalid_at: toString(r.invalid_at),
                expired_at: toString(r.expired_at)
            }})[0..20] AS facts
            RETURN episode_uuid, facts
        """
        params: dict[str, Any] = {
            "episode_uuids": safe_uuids,
            **tenant_scope_params(namespace),
        }
        rows = self.neo4j.execute(query, params=params)
        return {str(row["episode_uuid"]): list(row.get("facts") or []) for row in rows}

    def resolve_timeline_subject(
        self, *, subject: str, namespace: str | None
    ) -> list[dict[str, Any]]:
        """Resolve a subject thread anchor: an entity uuid or an exact case-insensitive
        name among visible non-View `:Entity` nodes in scope (A3).

        Returns up to 5 `{uuid, name}` candidates; the caller decides between unique,
        ambiguous, and unknown. Read-only.
        """
        where = [
            "(n.uuid = $subject OR (n.name IS NOT NULL AND toLower(n.name) = toLower($subject)))",
            non_derived_view_cypher("n"),
            default_recall_visibility_cypher("n", include_retired=True),
        ]
        params: dict[str, Any] = {"subject": str(subject).strip(), "limit": 5}
        params.update(tenant_scope_params(namespace))
        if namespace_spellings(namespace) is not None:
            where.insert(0, tenant_scope_prefilter_cypher("n"))
        where.append(tenant_scope_cypher("n"))
        query = f"""
            MATCH (n:Entity)
            WHERE {" AND ".join(where)}
            RETURN n.uuid AS uuid, n.name AS name
            ORDER BY n.uuid
            LIMIT $limit
        """
        return self.neo4j.execute(query, params=params)

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
        mirroring `search_episode_embeddings` but over the assertion log. Only MATERIALIZABLE observations
        are candidates -- current (`NOT superseded`) and bound (`NOT binding_pending`); a superseded or
        unbound row must never surface. Scoped by `namespace` (assertions carry `namespace`, not
        `group_id`). Returns the assertion id + `stated_span` (the recall surface / user's own words) +
        cosine + the slot/subject identity a downstream deterministic View-authority lookup needs."""
        safe_limit = max(1, min(limit, 500))
        where = [
            "a.name_embedding IS NOT NULL",
            # A stale-dimension embedding is skipped, not raised on (mirrors search_episode_embeddings);
            # `where` feeds both the scan and the index-candidate post-filter.
            "size(a.name_embedding) = size($query_vector)",
            "NOT coalesce(a.superseded, false)",
            "NOT coalesce(a.binding_pending, false)",
            "($namespaces IS NULL OR a.namespace IN $namespaces)",
        ]
        tail = """
            WITH a, vector.similarity.cosine(a.name_embedding, $query_vector) AS cosine
            WHERE cosine IS NOT NULL
            RETURN a.assertion_id AS assertion_id, a.stated_span AS stated_span, cosine,
                   a.subject_uuid AS subject_uuid, a.subject_display AS subject_display,
                   a.attribute AS attribute, a.scope AS scope, a.value_kind AS value_kind,
                   a.unit AS unit, a.namespace AS namespace, a.evidence_tier AS evidence_tier,
                   a.operation AS operation, a.value AS value, toString(a.valid_at) AS valid_at,
                   coalesce(a.count_object, '') AS count_object,
                   coalesce(a.count_family, '') AS count_family
            ORDER BY cosine DESC, a.assertion_id
            LIMIT $limit
            """
        params = {"query_vector": query_vector, "namespaces": namespaces, "limit": safe_limit}
        rows = self._index_backed(
            TYPED_ASSERTION_NAME_EMBEDDING, "a", where, tail, params, query_vector, safe_limit
        )
        if rows is not None:
            return rows
        return self.neo4j.execute(
            f"MATCH (a:TypedAssertion) WHERE {' AND '.join(where)}" + tail, params=params
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
            .return_fields((*metadata_fields, candidate_verifier_evidence_cypher()))
            .build())
        return self.neo4j.execute(
            query,
            params={"uuids": node_uuids, "belief_commit_key": "belief_commit"},
        )

    def fetch_temporal_facts(
        self, node_uuids: list[str], *, namespace: str | None = None,
    ) -> list[dict[str, object]]:
        """Fetch bi-temporal fact-edge state for candidate entity nodes.

        Only edges with fact text count: link edges without it would take the per-node slots
        (and mark a node as having temporal facts for the belief gate).

        ``namespace`` scopes the fact edge itself, as ``timeline_facts`` does: an entity
        admitted in the caller's namespace can still carry a fact written from another
        namespace's episode, and a scoped recall must not show it. ``None`` = no filter.
        """
        if not node_uuids:
            return []
        query = (Cypher()
            .match("(n:Entity)-[r:RELATES_TO]-(m:Entity)")
            .where(
                f"n.uuid IN $uuids AND {FACT_TEXT_PRESENT} AND {FACT_NOT_REDERIVE_PENDING} "
                f"AND NOT {memory_retired_cypher('n')} AND NOT {memory_retired_cypher('m')} "
                f"AND {tenant_scope_cypher('r')}"
            )
            .return_fields(FACT_TEMPORAL_FIELDS)
            .build())
        return self.neo4j.execute(
            query, params={"uuids": node_uuids, **tenant_scope_params(namespace)},
        )

    def fetch_candidate_fact_edges(self, node_uuids: list[str]) -> list[dict[str, object]]:
        """Fetch fact-EDGE identity (not just node state) for candidate entity nodes.

        Stage 1 of shadow-mode context composition
        (.agent/archive/plans/menhir-context-composition-production-integration.md) selects at
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
            .where(
                f"n.uuid IN $uuids AND {FACT_TEXT_PRESENT} AND {FACT_NOT_REDERIVE_PENDING} "
                f"AND NOT {memory_retired_cypher('n')} AND NOT {memory_retired_cypher('m')}"
            )
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
            WITH n,
                 [ (n)-[r]-()
                   WHERE type(r) = $relates_to_type
                     AND r[$fact_key] IS NOT NULL AND trim(toString(r[$fact_key])) <> ''
                     AND NOT coalesce(r[$rederive_pending_key], false)
                     AND (
                       (coalesce(r.namespace, r.group_id, '') IN ['', 'default']
                        AND coalesce(n.namespace, n.group_id, '') IN ['', 'default'])
                       OR coalesce(r.namespace, r.group_id, '') = coalesce(n.namespace, n.group_id, '')
                     )
                   | {
                       valid_at: toString(r[$valid_at_key]),
                       invalid_at: toString(r[$invalid_at_key]),
                       expired_at: toString(r[$expired_at_key])
                     } ] AS _fe
            WITH n, _fe, [e IN _fe WHERE e.expired_at IS NULL] AS _active_fe
            RETURN n.uuid AS uuid,
                   [ (n)-[supported_by]->(ev:Evidence)
                     WHERE type(supported_by) = $supported_by_type
                     | ev[$evidence_kind_key] ] AS evidence_node_kinds,
                   [ (n)-[:ANCHORED_TO]->(st:Entity)
                     WHERE st.structure_role IS NOT NULL | st.structure_project ] AS anchor_projects,
                   [ (n)-[:ANCHORED_TO]->(st:Entity)
                     WHERE st.structure_role IS NOT NULL | st.structure_path ] AS anchor_paths,
                   [ (epi:Episodic)-[:MENTIONS]->(n) | epi.source ] AS episode_sources,
                   [ (wa)-[about_rel]->(n)
                     WHERE type(about_rel) = $about_type
                       AND $wa_label IN labels(wa)
                       AND (
                         (coalesce(wa[$wa_ns_key], '') IN ['', 'default']
                          AND coalesce(n.namespace, n.group_id, '') IN ['', 'default'])
                         OR coalesce(wa[$wa_ns_key], '') = coalesce(n.namespace, n.group_id, '')
                       )
                     | {
                         status: wa[$wa_status_key],
                         artifact_type: wa[$wa_type_key],
                         superseded_by: head([ (rep)-[sup_rel]->(wa)
                                               WHERE type(sup_rel) = $supersedes_type
                                                 AND $wa_label IN labels(rep)
                                               | rep[$wa_uuid_key] ])
                       } ] AS work_artifacts,
                   {
                     total: size(_fe),
                     active: size(_active_fe),
                     expired: size(_fe) - size(_active_fe),
                     valid_ats: CASE
                       WHEN size(_active_fe) > 0 AND size(_active_fe) <= 64
                            AND all(e IN _active_fe WHERE e.valid_at IS NOT NULL)
                       THEN [e IN _active_fe | e.valid_at]
                       ELSE []
                     END,
                     invalid_ats: CASE
                       WHEN size(_active_fe) > 0 AND size(_active_fe) <= 64
                            AND all(e IN _active_fe WHERE e.invalid_at IS NOT NULL)
                       THEN [e IN _active_fe | e.invalid_at]
                       ELSE []
                     END
                   } AS fact_temporal
        """
        return self.neo4j.execute(
            query,
            params={
                "uuids": node_uuids,
                "relates_to_type": "RELATES_TO",
                "fact_key": "fact",
                "rederive_pending_key": "rederive_pending",
                "valid_at_key": "valid_at",
                "invalid_at_key": "invalid_at",
                "expired_at_key": "expired_at",
                "supported_by_type": "SUPPORTED_BY",
                "evidence_kind_key": "kind",
                "wa_label": "WorkArtifact",
                "about_type": "ABOUT",
                "supersedes_type": "SUPERSEDES",
                "wa_ns_key": "namespace",
                "wa_status_key": "status",
                "wa_type_key": "artifact_type",
                "wa_uuid_key": "artifact_uuid",
            },
        )

    def fetch_result_provenance(
        self, uuids: list[str], namespace: str | None
    ) -> list[dict[str, object]]:
        """Raw "who said it" signals for recall result ids, in ONE batched query.

        Each id resolves as exactly one of: an Entity (contributing episodes are the
        ``MENTIONS`` episodes), a RELATES_TO fact edge (episodes whose uuid is in
        ``r.episodes``), or a pending anchor (the anchor is itself the episode). Entities and
        edges are tenant-scoped to ``namespace`` (no filter when None);
        anchors are scoped the same way.

        Per contributing Graphiti episode the query joins Menhir's receipt anchor
        (``receipt.resolved_episode_uuid = epi.uuid``, indexed; see ``fetch_node_receipts``):
        source is ``coalesce(receipt.source, epi.source)`` (Graphiti stamps a placeholder
        ``'message'`` on ``epi`` until Menhir overwrites it, so the receipt wins), first-seen is
        ``coalesce(receipt.reference_time, epi.valid_at, epi.created_at)`` and the turn ids come
        from the receipt's ``ADMITTED_ON`` edges. Returns one row per matched id with
        ``uuid``, ``sources`` (distinct, may include null), ``first_seen_at`` (min, string) and
        ``turn_evidence_ids`` (distinct, at most 5). Ids matching nothing, and entities or edges
        with no contributing episode (Views, projection- or scan-built nodes), return no row, so
        they carry no provenance and no label. Raw signals only -- classification is in the
        domain (``derive_recall_provenance``)."""
        if not uuids:
            return []
        query = f"""
            UNWIND $uuids AS id
            CALL {{
              WITH id
              MATCH (n:Entity {{uuid: id}}) WHERE {tenant_scope_cypher('n')}
              MATCH (epi:Episodic)-[:MENTIONS]->(n)
              RETURN epi AS epi, null AS anchor
              UNION
              WITH id
              MATCH ()-[r:RELATES_TO {{uuid: id}}]->() WHERE {tenant_scope_cypher('r')}
              MATCH (epi:Episodic) WHERE epi.uuid IN r.episodes
              RETURN epi AS epi, null AS anchor
              UNION
              WITH id
              MATCH (a:Episodic {{uuid: id}}) WHERE {tenant_scope_cypher('a')}
              RETURN null AS epi, a AS anchor
            }}
            OPTIONAL MATCH (receipt:Episodic)
              WHERE epi IS NOT NULL AND receipt.resolved_episode_uuid = epi.uuid
            WITH id, epi, coalesce(receipt, anchor) AS rec
            WITH id, rec,
                 coalesce(rec.source, epi.source) AS source,
                 toString(coalesce(rec.reference_time, epi.valid_at, epi.created_at)) AS first_seen
            WITH id,
                 collect(DISTINCT source) AS sources,
                 min(first_seen) AS first_seen_at,
                 collect({{rec: rec, fs: first_seen}}) AS eps
            UNWIND eps AS e
            WITH id, sources, first_seen_at, e ORDER BY e.fs
            WITH id, sources, first_seen_at, collect(e.rec)[0..5] AS earliest
            RETURN id AS uuid, sources, first_seen_at,
                   reduce(acc = [], r IN earliest |
                          acc + [ (r)-[:ADMITTED_ON]->(t:TurnEvidence) | t.turn_id ])
                     AS all_turn_ids
        """
        rows = self.neo4j.execute(
            query,
            params={"uuids": uuids, **tenant_scope_params(namespace)},
            timeout_s=10.0,
        )
        for row in rows:
            seen: list[object] = []
            for t in row.pop("all_turn_ids", None) or []:
                if t is not None and t not in seen:
                    seen.append(t)
            row["turn_evidence_ids"] = seen[:5]
        return rows

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
                   coalesce(n['view_name'], n.name) AS name,
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
        # The shared tenant predicate, so a node scoped only by group_id (or by the legacy ''
        # default) is matched; a no-op when namespace is None.
        query += f"""
              AND {tenant_scope_cypher("a")}
              AND {tenant_scope_cypher("b")}
            RETURN DISTINCT a.uuid AS source, b.uuid AS target,
                   r.weight AS weight, r.uuid AS edge_uuid
            """
        params = {"all_uuids": all_uuids, **tenant_scope_params(namespace)}
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
                n.bootstrap_scope = null,
                n.pin_priority = null
            RETURN count(n) AS unflagged
            """
        )
        return int(rows[0].get("unflagged", 0)) if rows else 0

    # --- Flag & delete ------------------------------------------------------

    def flag_memory(
        self,
        node_uuid: str,
        bootstrap_scope: str | None = None,
        pin_priority: int | None = None,
    ) -> bool:
        """Persist the explicit v1 retention override on a node.

        Rejects structural graph nodes (project-scan entities) because they
        are not semantic memories and waste bootstrap tokens. ``pin_priority``
        (0-100) orders startup pins; ``None`` keeps the stored value.
        """

        normalized_priority = normalize_pin_priority(pin_priority)
        # Guard query: a structural entity, or a retired memory (either half of an episode pair),
        # cannot be flagged. Retire refuses flagged memories, so flag must refuse retired ones or
        # flag-after-retire yields a hidden pin. Rows come back only when a guard fires.
        guard = self.neo4j.execute(
            f"""
            {self._MEMORY_BY_UUID}
            {self._EPISODE_PAIR_MATCH}
            WITH n, (n:Entity AND NOT ({non_structural_memory_cypher("n")})) AS structural,
                 any(x IN nodes WHERE {memory_retired_cypher('x')}) AS retired
            WHERE structural OR retired
            RETURN CASE WHEN structural
                        THEN coalesce(n.structure_role, 'legacy_project_scan') END AS role,
                   retired
            LIMIT 1
            """,
            params={"node_uuid": node_uuid},
        )
        if guard and guard[0].get("role"):
            role = guard[0].get("role", "unknown")
            raise ValueError(
                f"Cannot flag structural graph node (role={role}). "
                "Flag semantic memories only."
            )
        if guard and guard[0].get("retired"):
            raise ValueError(
                "Cannot flag a retired memory. Restore it first (restore_memory); an entity hidden "
                "because every memory mentioning it is retired comes back when one of those is "
                "restored."
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
        priority_set = ""
        if normalized_priority is not None:
            priority_set = ",\n                n.pin_priority = $pin_priority"
            params["pin_priority"] = normalized_priority
        rows = self.neo4j.execute(
            f"""
            MATCH (n)
            WHERE (n:Entity OR n:Episodic) AND n.uuid = $node_uuid
              AND NOT {memory_retired_cypher('n')}
            SET n.user_flagged = true,
                {scope_set}{priority_set}
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
                n.bootstrap_scope = null,
                n.pin_priority = null
            RETURN count(n) AS nodes_updated
            """,
            params={"node_uuid": node_uuid},
        )
        return bool(rows and int(rows[0].get("nodes_updated", 0)) > 0)

    def promote_memory(self, node_uuid: str) -> bool:
        """Promote a PERSISTENT memory to PROMOTED: operator-curated, verified ground truth (SSOT-08).

        Distinct from user_flagged (direct retention intent, decay-immune but still
        an ordinary claim): PROMOTED is a stronger,
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

    # --- Retire / restore (visibility only) ----------------------------------

    #: Label-anchored lookup by uuid (an unlabelled `MATCH (n)` would scan every node).
    _MEMORY_BY_UUID = (
        "CALL { MATCH (x:Entity {uuid: $node_uuid}) RETURN x "
        "UNION MATCH (x:Episodic {uuid: $node_uuid}) RETURN x } "
        "WITH x AS n "
    )
    #: The resolved-twin pair of an episode (queue node <-> Graphiti episode), both directions,
    #: as two index-backed matches (an OR between them would defeat both indexes).
    _EPISODE_PAIR_MATCH = (
        "OPTIONAL MATCH (pa:Episodic {uuid: n.resolved_episode_uuid}) "
        "WHERE n:Episodic AND pa.uuid <> n.uuid "
        "OPTIONAL MATCH (pb:Episodic) "
        "WHERE n:Episodic AND pb.resolved_episode_uuid = n.uuid AND pb.uuid <> n.uuid "
        "WITH n, pa, collect(pb) AS pbs "
        "WITH n, [n] + CASE WHEN pa IS NULL THEN [] ELSE [pa] END + pbs AS nodes "
    )

    def retire_memory(
        self, node_uuid: str, *, reason: str = "", retired_by: str = ""
    ) -> dict[str, Any]:
        """Hide a memory from default recall without touching anything else (retire_memory).

        Sets ``memory_retired`` (and at/reason/by) on the node and, for an episode, on its
        queue-node/resolved-twin pair. It never touches edges, assertions, Views, counts or
        history. Returns ``{"status": "retired" | "already_retired", "uuid", "retired_at"}``,
        ``{"status": "not_found"}``, or ``{"status": "refused", "code", "detail"}`` where code is
        one of ``structural``, ``is_view``, ``candidate_scope``, ``episode_in_flight``,
        ``user_flagged``, ``retention_protected`` (or ``changed`` if the node moved between the
        check and the write). Idempotent: a retired memory keeps its first ``retired_at``.
        """
        # G-q: refuse any node of the pair with an erase marker, erased_at or deleted_by_user, in
        # the probe and again in the write, so retire never writes its reason onto erased text.
        not_erased_x = (
            "(x['erased_at'] IS NULL "
            "AND coalesce(x.processing_substage, '') <> 'deleted_by_user' "
            f"AND {marker_absent_cypher('x')})"
        )
        # The write marks every node of an episode pair, so every guard is checked on every node.
        in_flight_x = (
            "(x:Episodic AND coalesce(x.processing_state, '') IN ['PENDING', 'ENRICHING'])"
        )
        probe = self.neo4j.execute(
            f"""
            {self._MEMORY_BY_UUID}
            {self._EPISODE_PAIR_MATCH}
            RETURN n.uuid AS uuid,
                   any(x IN nodes WHERE NOT ({non_structural_memory_cypher("x")})) AS structural,
                   any(x IN nodes WHERE NOT ({non_derived_view_cypher("x")})) AS is_view,
                   any(x IN nodes WHERE NOT {not_erased_x}) AS erased,
                   any(x IN nodes WHERE coalesce(x.scope, 'PERSISTENT') = 'CANDIDATE')
                       AS candidate,
                   any(x IN nodes WHERE {in_flight_x}) AS in_flight,
                   any(x IN nodes WHERE coalesce(x.user_flagged, false)) AS user_flagged,
                   all(x IN nodes WHERE {destructive_retention_allowed_cypher("x")})
                       AS retention_allowed,
                   all(x IN nodes WHERE coalesce(x.memory_retired, false)) AS already_retired,
                   toString(n.memory_retired_at) AS retired_at
            LIMIT 1
            """,
            params={"node_uuid": node_uuid},
        )
        if not probe:
            return {"status": "not_found", "uuid": node_uuid}
        row = probe[0]
        refusal = None
        if row.get("erased"):
            refusal = ("erased", "the memory is erased or being erased")
        elif row.get("structural"):
            refusal = ("structural", "a structural graph node is not a semantic memory")
        elif row.get("is_view"):
            refusal = ("is_view", "a View is derived state and cannot be retired")
        elif row.get("candidate"):
            refusal = ("candidate_scope", "a CANDIDATE memory is not recallable; review it instead")
        elif row.get("in_flight"):
            refusal = ("episode_in_flight", "the episode is still PENDING/ENRICHING")
        elif row.get("user_flagged"):
            refusal = ("user_flagged", "the memory is flagged; unflag it first")
        elif not row.get("retention_allowed"):
            refusal = (
                "retention_protected",
                "the memory is retention-protected; unflag its source first",
            )
        if refusal is not None:
            return {"status": "refused", "uuid": node_uuid, "code": refusal[0], "detail": refusal[1]}
        rows = self.neo4j.execute(
            f"""
            {self._MEMORY_BY_UUID}
            {self._EPISODE_PAIR_MATCH}
            WITH n, nodes
            WHERE all(x IN nodes WHERE {non_structural_memory_cypher("x")})
              AND all(x IN nodes WHERE {non_derived_view_cypher("x")})
              AND all(x IN nodes WHERE {not_erased_x})
              AND none(x IN nodes WHERE coalesce(x.scope, 'PERSISTENT') = 'CANDIDATE')
              AND none(x IN nodes WHERE {in_flight_x})
              AND none(x IN nodes WHERE coalesce(x.user_flagged, false))
              AND all(x IN nodes WHERE {destructive_retention_allowed_cypher("x")})
            WITH n, nodes,
                 all(x IN nodes WHERE coalesce(x.memory_retired, false)) AS already,
                 [x IN nodes WHERE NOT coalesce(x.memory_retired, false)] AS to_mark
            FOREACH (x IN to_mark |
                SET x.memory_retired = true,
                    x.memory_retired_at = datetime(),
                    x.memory_retired_reason = $reason,
                    x.memory_retired_by = $retired_by)
            RETURN n.uuid AS uuid, already, toString(n.memory_retired_at) AS retired_at
            LIMIT 1
            """,
            params={"node_uuid": node_uuid, "reason": reason, "retired_by": retired_by},
        )
        if not rows:
            return {"status": "refused", "uuid": node_uuid, "code": "changed",
                    "detail": "the memory changed state while retiring; retry"}
        return {
            "status": "already_retired" if rows[0].get("already") else "retired",
            "uuid": node_uuid,
            "retired_at": rows[0].get("retired_at"),
        }

    def restore_memory(self, node_uuid: str) -> dict[str, Any]:
        """Undo ``retire_memory``: clear the marker on the node (and its episode twin pair).

        Keeps ``memory_retired_at`` as history, stamps ``memory_restored_at`` and removes the
        reason/by. Returns ``{"status": "restored" | "not_retired" | "not_found", "uuid"}``.
        """
        rows = self.neo4j.execute(
            f"""
            {self._MEMORY_BY_UUID}
            {self._EPISODE_PAIR_MATCH}
            WITH n, [x IN nodes WHERE coalesce(x.memory_retired, false)] AS retired_nodes
            FOREACH (x IN retired_nodes |
                SET x.memory_retired = false,
                    x.memory_restored_at = datetime()
                REMOVE x.memory_retired_reason, x.memory_retired_by)
            RETURN n.uuid AS uuid, size(retired_nodes) > 0 AS was_retired
            LIMIT 1
            """,
            params={"node_uuid": node_uuid},
        )
        if not rows:
            return {"status": "not_found", "uuid": node_uuid}
        return {
            "status": "restored" if rows[0].get("was_retired") else "not_retired",
            "uuid": node_uuid,
        }

    # CF-330: the scrub removes these too. Literal lists, so every call site gets them.
    _EDGE_ANCHORED_TIME_PROPS = "[" + ", ".join(f'"{p}"' for p in PRESERVED_EDGE_ATTRIBUTES) + "]"
    _ENTITY_KEEP_PROPS = "[" + ", ".join(
        f'"{p}"' for p in sorted(ENTITY_STRUCTURAL_PROPERTIES | {"summary"})
    ) + "]"

    # CF-332: conflict resolution appended an erased memory's content to a kept entity, as a block
    # headed by the marker below. Cut those blocks out (the holder keeps its own text), blank the
    # summary that may restate them and flag the holder. Legacy holders have no absorbed_from, so
    # the marker text is the fallback selector. A block is a copy of its source entity's text, so
    # it goes whenever that text goes: the erase targets, and in the cascade also every entity it
    # deleted or scrubbed (__CUT_SET__). Shared by the cascade and the target-absent path.
    _HOLDER_SCRUB_FRAGMENT = (
        """
        WITH __CARRY__ __CUT_SET__ AS cut_uuids
        OPTIONAL MATCH (h:Entity)
        WHERE NOT h.uuid IN $erased_uuids AND NOT h.uuid IN __DELETED__
          AND (any(u IN coalesce(h.absorbed_from, []) WHERE u IN cut_uuids)
               OR any(u IN cut_uuids
                      WHERE coalesce(h.content, '') CONTAINS __ABSORB_SEP__ + u + ')'
                         OR coalesce(h.original_content, '') CONTAINS __ABSORB_SEP__ + u + ')'))
          __HOLDER_NS_FILTER__
        WITH __CARRY__ cut_uuids,
             [x IN collect(DISTINCT h) WHERE x IS NOT NULL] AS holders
        FOREACH (h IN holders |
            SET h.content = __CUT__(h.content),
                h.original_content = __CUT__(h.original_content),
                h.summary = '',
                h.absorbed_from = [u IN coalesce(h.absorbed_from, []) WHERE NOT u IN cut_uuids],
                h.rederive_pending = true,
                h.rederive_reason = 'source_erased',
                h.rederive_flagged_at = datetime()
            REMOVE h.content_embedding
        )
        FOREACH (h IN holders |
            MERGE (rr:DerivedRederiveRepair {repair_key: $node_uuid + '\\u001f' + h.uuid})
            ON CREATE SET rr.item_uuid = h.uuid,
                          rr.item_kind = 'entity',
                          rr.namespace = CASE
                              WHEN trim(toString(coalesce(h.namespace, h.group_id, ''))) = ''
                              THEN 'default'
                              ELSE trim(toString(coalesce(h.namespace, h.group_id)))
                          END,
                          rr.status = 'pending',
                          rr.created_at = datetime()
        )
        """
    ).replace(
        "__CUT__(h.content)", _absorbed_block_cut("h.content")
    ).replace(
        "__CUT__(h.original_content)", _absorbed_block_cut("h.original_content")
    ).replace("__ABSORB_SEP__", ABSORBED_BLOCK_SEPARATOR_CYPHER)

    # Derived-fact cleanup (#423). Fact edges first: strip erased episodes, delete emptied edges,
    # flag survivors. The entity step reads the stripped edges, so this must run before it.
    _DERIVED_EDGE_BODY = (
        """
        OPTIONAL MATCH (:Entity)-[fe:RELATES_TO]->(:Entity)
        WHERE fe.fact IS NOT NULL
          AND any(u IN coalesce(fe.episodes, []) WHERE u IN $erased_uuids)
          __EDGE_NS_FILTER__
        WITH [x IN collect(DISTINCT fe) WHERE x IS NOT NULL] AS hit_edges
        WITH [x IN hit_edges
              WHERE size([u IN coalesce(x.episodes, []) WHERE NOT u IN $erased_uuids]) = 0
             ] AS dead_edges,
             [x IN hit_edges
              WHERE size([u IN coalesce(x.episodes, []) WHERE NOT u IN $erased_uuids]) > 0
             ] AS live_edges
        WITH dead_edges, live_edges,
             [x IN dead_edges | coalesce(x.uuid, elementId(x))] AS edges_deleted_uuids,
             [x IN live_edges | coalesce(x.uuid, elementId(x))] AS edges_flagged_uuids
        // Owner ruling 2026-11-09: scrub at erase. The text may restate the erased episode; the
        // rederive job rebuilds it from the remaining sources ('' keeps Graphiti's str field valid).
        FOREACH (x IN live_edges |
            SET x.episodes = [u IN coalesce(x.episodes, []) WHERE NOT u IN $erased_uuids],
                x.fact = '',
                x.fact_embedding = null,
                x.rederive_pending = true,
                x.rederive_reason = 'source_erased',
                x.rederive_flagged_at = datetime()
        )
        // CF-330: time_expression quotes the memory; the rest of the anchored-time contract was
        // resolved from the scrubbed fact, so it goes with it.
        FOREACH (x IN live_edges |
            FOREACH (k IN [k IN keys(x) WHERE k IN __EDGE_TIME_PROPS__] | REMOVE x[k])
        )
        FOREACH (x IN live_edges |
            MERGE (rr:DerivedRederiveRepair {
                repair_key: $node_uuid + '\\u001f' + coalesce(x.uuid, elementId(x))
            })
            ON CREATE SET rr.item_uuid = coalesce(x.uuid, elementId(x)),
                          rr.item_kind = 'fact',
                          rr.namespace = CASE
                              WHEN trim(toString(coalesce(x.namespace, x.group_id, ''))) = ''
                              THEN 'default'
                              ELSE trim(toString(coalesce(x.namespace, x.group_id)))
                          END,
                          rr.status = 'pending',
                          rr.created_at = datetime()
        )
        FOREACH (x IN dead_edges | DELETE x)
        // CF-331: a conflict record copies its kept and cause facts. It goes when either edge
        // was deleted or scrubbed, or when an erased episode caused it (the cause edge may never
        // have persisted).
        WITH edges_deleted_uuids, edges_flagged_uuids
        OPTIONAL MATCH (c:EdgeInvalidationConflict)
        WHERE (c.kept_edge_uuid IN edges_deleted_uuids + edges_flagged_uuids
               OR c.cause_edge_uuid IN edges_deleted_uuids + edges_flagged_uuids
               OR any(u IN coalesce(c.cause_episode_uuids, []) WHERE u IN $erased_uuids))
          __CONFLICT_NS_FILTER__
        WITH edges_deleted_uuids, edges_flagged_uuids,
             [x IN collect(DISTINCT c) WHERE x IS NOT NULL] AS conflicts
        FOREACH (x IN conflicts | DETACH DELETE x)
        RETURN edges_deleted_uuids, edges_flagged_uuids, size(conflicts) AS conflicts_deleted
        """
    ).replace("__EDGE_TIME_PROPS__", _EDGE_ANCHORED_TIME_PROPS)
    _DERIVED_EDGE_NS_FILTER = (
        """AND CASE
                 WHEN trim(toString(coalesce(fe.namespace, fe.group_id, ''))) = ''
                 THEN 'default'
                 ELSE trim(toString(coalesce(fe.namespace, fe.group_id)))
             END = $namespace_key"""
    )
    _DERIVED_CONFLICT_NS_FILTER = _DERIVED_EDGE_NS_FILTER.replace(
        "coalesce(fe.namespace, fe.group_id", "coalesce(c.group_id"
    )
    _DERIVED_EDGE_SCOPED_BODY = _DERIVED_EDGE_BODY.replace(
        "__EDGE_NS_FILTER__", _DERIVED_EDGE_NS_FILTER
    ).replace("__CONFLICT_NS_FILTER__", _DERIVED_CONFLICT_NS_FILTER)
    _DERIVED_EDGE_CALL = "\n            CALL {" + _DERIVED_EDGE_SCOPED_BODY + "}\n"
    # Target gone: its fact edges and its absorbed blocks (CF-332) can still carry the text. One
    # statement, so the holder scrub commits with the edge scrub.
    _DERIVED_EDGE_RETURN = (
        "RETURN edges_deleted_uuids, edges_flagged_uuids, size(conflicts) AS conflicts_deleted"
    )
    _ABSENT_HOLDER_TAIL = (
        "WITH edges_deleted_uuids, edges_flagged_uuids, size(conflicts) AS conflicts_deleted"
        + _HOLDER_SCRUB_FRAGMENT.replace("__DELETED__", "[]").replace(
            "__CARRY__", "edges_deleted_uuids, edges_flagged_uuids, conflicts_deleted,"
        ).replace("__CUT_SET__", "$erased_uuids")
        + "RETURN edges_deleted_uuids, edges_flagged_uuids, conflicts_deleted,\n"
        "               [h IN holders | h.uuid] AS holders_flagged_uuids\n"
    )
    _DERIVED_EDGE_ONLY_STATEMENT = _DERIVED_EDGE_BODY.replace("__EDGE_NS_FILTER__", "").replace(
        "__CONFLICT_NS_FILTER__", ""
    ).replace(_DERIVED_EDGE_RETURN, _ABSENT_HOLDER_TAIL.replace("__HOLDER_NS_FILTER__", ""))
    # Target gone, caller scoped: only edges and holders in the caller's namespace (review F4).
    _DERIVED_EDGE_ONLY_SCOPED_STATEMENT = _DERIVED_EDGE_SCOPED_BODY.replace(
        _DERIVED_EDGE_RETURN,
        _ABSENT_HOLDER_TAIL.replace(
            "__HOLDER_NS_FILTER__", _DERIVED_EDGE_NS_FILTER.replace("fe.", "h.")
        ),
    )
    # Entities reached from the target episode; views/artifacts/structure/TEMPORAL are skipped.
    _DERIVED_ENTITY_MATCH = (
        """
        OPTIONAL MATCH (:Episodic {uuid: $node_uuid})-[:MENTIONS|RETENTION_SOURCE]->(e:Entity)
        WHERE NOT (coalesce(e.is_view, false)
                   OR coalesce(e.is_quantstate, false)
                   OR e.view_kind IS NOT NULL)
          AND NOT coalesce(e.is_artifact, false)
          AND coalesce(e.type, '') <> 'TEMPORAL'
          AND ("""
        + non_structural_memory_cypher("e")
        + """)
          AND NOT e.uuid IN $erased_uuids
          __ENTITY_NS_FILTER__
        WITH collect(DISTINCT e) AS cands
        WITH cands,
             [e IN cands WHERE EXISTS {
                 MATCH (e)-[r:RELATES_TO]-(:Entity)
                 WHERE r.fact IS NOT NULL AND size(coalesce(r.episodes, [])) = 0
             }] AS unknown
        WITH [e IN cands WHERE NOT e IN unknown] AS rest, size(unknown) AS unknown_n
        WITH rest, unknown_n,
             [e IN rest
              WHERE NOT EXISTS {
                      MATCH (o:Episodic)-[:MENTIONS|RETENTION_SOURCE]->(e)
                      WHERE NOT o.uuid IN $erased_uuids
                  }
                AND NOT EXISTS {
                      MATCH (e)-[r:RELATES_TO]-(:Entity)
                      WHERE r.fact IS NOT NULL AND size(coalesce(r.episodes, [])) > 0
                  }
                // Only entities the erasure fenced at PREPARE; the rest are flagged, not deleted.
                AND ($deletable_entity_uuids IS NULL OR e.uuid IN $deletable_entity_uuids)
             ] AS doomed
        WITH unknown_n, doomed, [e IN rest WHERE NOT e IN doomed] AS shared
        WITH unknown_n, doomed, shared,
             [e IN doomed | e.uuid] AS entities_deleted_uuids,
             [e IN shared | e.uuid] AS entities_flagged_uuids
        """
    )
    _DERIVED_ENTITY_NS_FILTER = (
        """AND CASE
                 WHEN trim(toString(coalesce(e.namespace, e.group_id, ''))) = ''
                 THEN 'default'
                 ELSE trim(toString(coalesce(e.namespace, e.group_id)))
             END = $namespace_key"""
    )
    _DERIVED_ENTITY_WRITE = (
        """
        // Scrub at erase (owner ruling 2026-11-09): summary/content may restate the erased episode.
        FOREACH (e IN shared |
            SET e.summary = '',
                e.content = null,
                e.rederive_pending = true,
                e.rederive_reason = 'source_erased',
                e.rederive_flagged_at = datetime()
        )
        // CF-330: every non-structural key is text (graph_text_inventory), e.g. original_content
        // and Graphiti custom attributes. Rederive restores only the summary (v2 S1.3).
        FOREACH (e IN shared |
            FOREACH (k IN [k IN keys(e) WHERE NOT k IN __ENTITY_KEEP_PROPS__
                                          AND NOT k STARTS WITH 'rederive_'] | REMOVE e[k])
        )
        FOREACH (e IN shared |
            MERGE (rr:DerivedRederiveRepair {repair_key: $node_uuid + '\\u001f' + e.uuid})
            ON CREATE SET rr.item_uuid = e.uuid,
                          rr.item_kind = 'entity',
                          rr.namespace = $namespace_key,
                          rr.status = 'pending',
                          rr.created_at = datetime()
        )
        FOREACH (e IN doomed | DETACH DELETE e)
        // CF-327: a raw capture is the erased episode's full text; delete it even when its
        // MENTIONS link is missing (legacy two-statement writer).
        WITH unknown_n, entities_deleted_uuids, entities_flagged_uuids
        OPTIONAL MATCH (rc:Entity)
        WHERE rc.raw_capture_for IN $erased_uuids AND NOT rc.uuid IN entities_deleted_uuids
          __RC_NS_FILTER__
        WITH unknown_n, entities_deleted_uuids, entities_flagged_uuids,
             [x IN collect(DISTINCT rc) WHERE x IS NOT NULL] AS rcs
        WITH unknown_n, rcs, entities_deleted_uuids + [x IN rcs | x.uuid] AS entities_deleted_uuids,
             [u IN entities_flagged_uuids WHERE NOT u IN [x IN rcs | x.uuid]]
                 AS entities_flagged_uuids
        FOREACH (x IN rcs | DETACH DELETE x)
        WITH unknown_n, entities_deleted_uuids, entities_flagged_uuids
        __HOLDER_SCRUB__
        RETURN unknown_n AS derived_unknown_provenance, entities_deleted_uuids,
               entities_flagged_uuids
               + [h IN holders WHERE NOT h.uuid IN entities_flagged_uuids | h.uuid]
               AS entities_flagged_uuids
        """
    ).replace("__ENTITY_KEEP_PROPS__", _ENTITY_KEEP_PROPS).replace(
        "__HOLDER_SCRUB__",
        _HOLDER_SCRUB_FRAGMENT.replace("__DELETED__", "entities_deleted_uuids").replace(
            "__CARRY__", "unknown_n, entities_deleted_uuids, entities_flagged_uuids,"
        ).replace(
            # toStringOrNull types the list as strings; Cypher's checker rejects `'...' + u`
            # over the cascade's computed lists otherwise.
            "__CUT_SET__",
            "[x IN $erased_uuids + entities_deleted_uuids + entities_flagged_uuids"
            " | toStringOrNull(x)]",
        ),
    )
    _DERIVED_ENTITY_CALL = (
        "\n            CALL {"
        + _DERIVED_ENTITY_MATCH.replace("__ENTITY_NS_FILTER__", _DERIVED_ENTITY_NS_FILTER)
        + _DERIVED_ENTITY_WRITE.replace(
            "__HOLDER_NS_FILTER__", _DERIVED_ENTITY_NS_FILTER.replace("e.", "h.")
        ).replace("__RC_NS_FILTER__", _DERIVED_ENTITY_NS_FILTER.replace("e.", "rc."))
        + "}\n"
    )

    # Re-derive repair (#423 follow-up): rewrite or delete items flagged `rederive_pending` by an
    # erasure. Text-bearing values are only ever passed as parameters; nothing here logs them.
    _REDERIVE_EDGE_MATCH = (
        "MATCH ()-[r:RELATES_TO]->() "
        "WHERE (r.uuid = $item_uuid OR (r.uuid IS NULL AND elementId(r) = $item_uuid))"
    )
    _REDERIVE_CLEAR = (
        "{v}.rederive_pending = null, {v}.rederive_reason = null, {v}.rederive_flagged_at = null"
    )

    def list_pending_derived_rederive(self, limit: int) -> list[dict[str, Any]]:
        """Pending DerivedRederiveRepair nodes, oldest first; a deferred one goes behind the rest.

        ``deferred_at`` is set when a provider fault stopped the batch on that repair (#874), so
        one item whose call always times out cannot stop every run at the head of the queue.
        """
        rows = self.neo4j.execute(
            """
            MATCH (rr:DerivedRederiveRepair)
            WHERE rr.status = 'pending'
            RETURN rr.repair_key AS repair_key, rr.item_uuid AS item_uuid,
                   rr.item_kind AS item_kind, rr.namespace AS namespace,
                   coalesce(rr.attempts, 0) AS attempts
            ORDER BY coalesce(rr.deferred_at, rr.created_at) ASC, rr.repair_key ASC
            LIMIT $limit
            """,
            params={"limit": int(limit)},
        )
        return [dict(row) for row in rows]

    def load_derived_rederive_item(self, item_kind: str, item_uuid: str) -> dict[str, Any] | None:
        """Snapshot a flagged item plus its remaining source episode contents.

        Returns None when the item no longer exists. ``episodes`` is the CAS token (with
        ``flagged_at``); ``sources`` is the content of the remaining source episodes.
        """
        if item_kind == "fact":
            rows = self.neo4j.execute(
                self._REDERIVE_EDGE_MATCH
                + """
                RETURN coalesce(r['rederive_pending'], false) AS pending,
                       toString(r['rederive_flagged_at']) AS flagged_at,
                       coalesce(r.episodes, []) AS episodes, r.fact AS text,
                       // Erase scrubs r.fact, so the claim's shape is what the rewrite checks.
                       coalesce(startNode(r).name, '?') + ' ' + coalesce(r.name, 'RELATES_TO')
                           + ' ' + coalesce(endNode(r).name, '?') AS name
                LIMIT 1
                """,
                params={"item_uuid": item_uuid},
            )
        else:
            rows = self.neo4j.execute(
                """
                MATCH (e:Entity {uuid: $item_uuid})
                RETURN coalesce(e['rederive_pending'], false) AS pending,
                       toString(e['rederive_flagged_at']) AS flagged_at,
                       [(ep:Episodic)-[:MENTIONS|RETENTION_SOURCE]->(e) | ep.uuid] AS episodes,
                       e.summary AS text, e.name AS name
                LIMIT 1
                """,
                params={"item_uuid": item_uuid},
            )
        if not rows:
            return None
        row = dict(rows[0])
        episodes_raw = [str(u) for u in (row.get("episodes") or [])]
        episodes = list(dict.fromkeys(str(u) for u in (row.get("episodes") or []) if u))
        source_rows = (
            self.neo4j.execute(
                """
                MATCH (ep:Episodic)
                WHERE ep.uuid IN $episodes AND ep.content IS NOT NULL
                RETURN ep.uuid AS uuid, ep.content AS content
                ORDER BY ep.uuid
                LIMIT 20
                """,
                params={"episodes": episodes},
            )
            if episodes
            else []
        )
        return {
            "pending": bool(row.get("pending")),
            "flagged_at": row.get("flagged_at"),
            "episodes": episodes,
            "episodes_raw": episodes_raw if item_kind == "fact" else episodes,
            "text": row.get("text"),
            "name": row.get("name"),
            "sources": [str(s.get("content") or "") for s in source_rows if s.get("content")],
        }

    def apply_derived_rederive(
        self,
        *,
        repair_key: str,
        item_kind: str,
        item_uuid: str,
        flagged_at: str | None,
        episodes: Sequence[str],
        new_text: str | None,
        delete: bool = False,
        embedding: Sequence[float] | None = None,
        embedding_missing: bool = False,
        reembed: bool = False,
    ) -> bool:
        """CAS write of a re-derive result; True only when it applied.

        Applies only if the item still has ``rederive_pending`` with the same
        ``rederive_flagged_at`` and the same source-episode set the caller read. The repair node
        is finished in the same statement ('done', or 'deleted' for a deleted fact edge).

        ``embedding_missing`` (fact rewrite whose embedder failed, #1005): the rewrite applies but
        the repair becomes 'embed_pending' instead of 'done'. ``reembed`` writes only the
        embedding of an 'embed_pending' repair's fact, and only while the edge is unflagged and its
        fact still equals ``new_text``; the repair then becomes 'done'.
        """
        params = {
            "repair_key": repair_key,
            "item_uuid": item_uuid,
            "flagged_at": flagged_at,
            "episodes": list(episodes),
            "new_text": new_text,
            "embedding": list(embedding) if embedding is not None else None,
            "embedding_missing": bool(embedding_missing),
        }
        edge_guard = (
            "coalesce(r['rederive_pending'], false) "
            "AND toString(r['rederive_flagged_at']) = $flagged_at "
            "AND coalesce(r.episodes, []) = $episodes"
        )
        if item_kind == "fact" and reembed:
            if embedding is None:
                return False
            query = (
                "MATCH (rr:DerivedRederiveRepair {repair_key: $repair_key}) "
                "WHERE rr.status = 'embed_pending' "
                + self._REDERIVE_EDGE_MATCH
                + " AND NOT coalesce(r['rederive_pending'], false) AND r.fact = $new_text "
                "SET r.fact_embedding = $embedding, rr.status = 'done', rr.finished_at = datetime() "
                "RETURN count(r) AS c"
            )
        elif item_kind == "fact" and delete:
            query = (
                "MATCH (rr:DerivedRederiveRepair {repair_key: $repair_key}) "
                + self._REDERIVE_EDGE_MATCH
                + f" AND {edge_guard} "
                "SET rr.status = 'deleted', rr.finished_at = datetime() "
                "WITH r, rr DELETE r RETURN count(rr) AS c"
            )
        elif item_kind == "fact":
            query = (
                "MATCH (rr:DerivedRederiveRepair {repair_key: $repair_key}) "
                + self._REDERIVE_EDGE_MATCH
                + f" AND {edge_guard} "
                "SET r.fact = $new_text, r.fact_embedding = $embedding, "
                + self._REDERIVE_CLEAR.format(v="r")
                + ", rr.status = CASE WHEN $embedding_missing THEN 'embed_pending' ELSE 'done' END, "
                "rr.finished_at = CASE WHEN $embedding_missing THEN null ELSE datetime() END "
                "RETURN count(r) AS c"
            )
        else:
            query = (
                "MATCH (rr:DerivedRederiveRepair {repair_key: $repair_key}) "
                "MATCH (e:Entity {uuid: $item_uuid}) "
                "WHERE coalesce(e['rederive_pending'], false) "
                "AND toString(e['rederive_flagged_at']) = $flagged_at "
                "WITH rr, e, [(ep:Episodic)-[:MENTIONS|RETENTION_SOURCE]->(e) | ep.uuid] AS cur "
                "WHERE all(x IN cur WHERE x IN $episodes) AND all(x IN $episodes WHERE x IN cur) "
                "SET e.summary = $new_text, "
                + self._REDERIVE_CLEAR.format(v="e")
                + ", rr.status = 'done', rr.finished_at = datetime() "
                "RETURN count(e) AS c"
            )
        rows = self.neo4j.execute(query, params=params)
        return bool(rows) and int(rows[0].get("c") or 0) > 0

    def mark_derived_rederive_status(self, repair_key: str, status: str) -> bool:
        """Set a pending (or embed_pending) repair's terminal status (e.g. 'obsolete' when the
        item is gone)."""
        rows = self.neo4j.execute(
            """
            MATCH (rr:DerivedRederiveRepair {repair_key: $repair_key})
            WHERE rr.status IN ['pending', 'embed_pending']
            SET rr.status = $status, rr.finished_at = datetime()
            RETURN count(rr) AS c
            """,
            params={"repair_key": repair_key, "status": status},
        )
        return bool(rows) and int(rows[0].get("c") or 0) > 0

    def defer_derived_rederive(self, repair_key: str) -> bool:
        """Move a pending (or embed_pending) repair behind the rest of its queue without spending
        an attempt (#874)."""
        rows = self.neo4j.execute(
            """
            MATCH (rr:DerivedRederiveRepair {repair_key: $repair_key})
            WHERE rr.status IN ['pending', 'embed_pending']
            SET rr.deferred_at = datetime()
            RETURN count(rr) AS c
            """,
            params={"repair_key": repair_key},
        )
        return bool(rows) and int(rows[0].get("c") or 0) > 0

    def list_embed_pending_derived_rederive(self, limit: int) -> list[dict[str, Any]]:
        """Rewritten facts whose embedding failed (#1005), oldest first, deferred ones last."""
        rows = self.neo4j.execute(
            """
            MATCH (rr:DerivedRederiveRepair)
            WHERE rr.status = 'embed_pending'
            RETURN rr.repair_key AS repair_key, rr.item_uuid AS item_uuid,
                   rr.item_kind AS item_kind
            ORDER BY coalesce(rr.deferred_at, rr.created_at) ASC, rr.repair_key ASC
            LIMIT $limit
            """,
            params={"limit": int(limit)},
        )
        return [dict(row) for row in rows]

    def requeue_failed_derived_rederive(
        self, cooldown_days: float, limit: int
    ) -> list[dict[str, Any]]:
        """Move up to ``limit`` 'failed' repairs whose last attempt is older than the cooldown back
        to 'pending' with attempts reset (#1005). Requeued repairs go behind the pending queue.

        Only the repair node changes; the repair key (erasure replay reads it by prefix) is kept.
        """
        rows = self.neo4j.execute(
            """
            MATCH (rr:DerivedRederiveRepair)
            WHERE rr.status = 'failed'
              AND coalesce(rr.last_attempt_at, rr.created_at)
                  < datetime() - duration({seconds: $cooldown_s})
            WITH rr
            ORDER BY coalesce(rr.last_attempt_at, rr.created_at) ASC, rr.repair_key ASC
            LIMIT $limit
            SET rr.status = 'pending', rr.attempts = 0, rr.deferred_at = datetime(),
                rr.requeued_at = datetime(), rr.requeue_count = coalesce(rr.requeue_count, 0) + 1
            RETURN rr.repair_key AS repair_key, rr.item_kind AS item_kind,
                   rr.last_error AS last_error, rr.requeue_count AS requeue_count
            """,
            params={"cooldown_s": int(float(cooldown_days) * 86400), "limit": int(limit)},
        )
        return [dict(row) for row in rows]

    def record_derived_rederive_failure(
        self, repair_key: str, error_class: str, max_attempts: int
    ) -> str | None:
        """Count a failed attempt (error class only, never content); 'failed' at the cap."""
        rows = self.neo4j.execute(
            """
            MATCH (rr:DerivedRederiveRepair {repair_key: $repair_key})
            WHERE rr.status = 'pending'
            WITH rr, coalesce(rr.attempts, 0) + 1 AS n
            SET rr.attempts = n, rr.last_error = $error_class,
                rr.last_attempt_at = datetime(),
                rr.status = CASE WHEN n >= $max_attempts THEN 'failed' ELSE 'pending' END
            RETURN rr.status AS status
            """,
            params={
                "repair_key": repair_key,
                "error_class": str(error_class)[:80],
                "max_attempts": int(max_attempts),
            },
        )
        return str(rows[0]["status"]) if rows else None

    def erase_target_namespace_keys(self, node_uuid: str) -> list[str]:
        """The canonical namespace keys of every node an erase of ``node_uuid`` addresses.

        Entity/Episodic by uuid, TurnEvidence by turn_id, TypedAssertion by its ids. Empty when the
        target is absent. The cascade and the coordinator's scope check share this (CF-326).
        """
        rows = self.neo4j.execute(
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
        return [
            str(value) for value in (
                rows[0].get("namespace_keys", []) if rows else []
            ) if value is not None and str(value).strip()
        ]

    def delete_memory_with_scalar_cascade(
        self,
        node_uuid: str,
        *,
        operation_id: str,
        erased_uuids: Sequence[str] | None = None,
        deletable_entity_uuids: Sequence[str] | None = None,
        scope_namespace: str | None = None,
    ) -> dict[str, Any]:
        """Delete one memory/observation and its authoritative scalar assertions atomically.

        A surfaced observation is addressed by ``assertion_id``; an Entity/Episodic deletion also
        removes assertions bound to or grounded on that node.  A pending repair receipt is created
        for every affected subject+namespace in the SAME Neo4j transaction as the deletion.  Thus a
        crash can delay View repair, but can never make the work undiscoverable (G15/G20).
        """
        namespace_keys = self.erase_target_namespace_keys(node_uuid)
        scope = (scope_namespace or "").strip()
        if scope and namespace_keys and namespace_keys != [normalize_namespace(scope)]:
            # CF-326: a scoped caller never cascades into another namespace, even when it names
            # an existing foreign node (e.g. a TurnEvidence turn_id the ownership guard missed).
            raise PermissionError(
                f"erase target resolves to namespace(s) {namespace_keys}, outside {scope!r}; "
                "refusing a cross-namespace cascade"
            )
        erased = list(dict.fromkeys(str(u) for u in (erased_uuids or [node_uuid]) if u))
        if not namespace_keys:
            result = self._empty_memory_erasure_result()
            if not erased:
                return result
            # Target already gone: its derived fact edges can still carry the erased text. A
            # scoped caller (pinned, or naming a namespace) touches only its own namespace: an
            # absent uuid passes the ownership guard, so it must not reach other silos (F4).
            scope = (scope_namespace or "").strip()
            if scope:
                edge_rows = self.neo4j.execute(
                    self._DERIVED_EDGE_ONLY_SCOPED_STATEMENT,
                    params={"node_uuid": node_uuid, "erased_uuids": erased,
                            "namespace_key": scope},
                )
            else:
                edge_rows = self.neo4j.execute(
                    self._DERIVED_EDGE_ONLY_STATEMENT,
                    params={"node_uuid": node_uuid, "erased_uuids": erased},
                )
            edge_row = dict(edge_rows[0]) if edge_rows else {}
            result["derived_edges_deleted_uuids"] = list(edge_row.get("edges_deleted_uuids") or [])
            result["derived_edges_flagged_uuids"] = list(edge_row.get("edges_flagged_uuids") or [])
            result["derived_edges_deleted"] = len(result["derived_edges_deleted_uuids"])
            result["derived_edges_flagged"] = len(result["derived_edges_flagged_uuids"])
            result["derived_conflicts_deleted"] = int(edge_row.get("conflicts_deleted") or 0)
            result["derived_entities_flagged_uuids"] = list(
                edge_row.get("holders_flagged_uuids") or []
            )
            result["derived_entities_flagged"] = len(result["derived_entities_flagged_uuids"])
            return result
        if len(namespace_keys) != 1:
            raise RuntimeError(
                "evidence erasure target resolves to multiple canonical namespaces; refusing "
                "an unfenced cross-namespace mutation"
            )
        return self._delete_memory_with_scalar_cascade_in_namespace(
            node_uuid,
            operation_id=operation_id,
            namespace_key=namespace_keys[0],
            erased_uuids=erased or [node_uuid],
            deletable_entity_uuids=deletable_entity_uuids,
        )

    @staticmethod
    def _empty_memory_erasure_result() -> dict[str, Any]:
        return {
            "derived_edges_deleted": 0,
            "derived_edges_flagged": 0,
            "derived_entities_deleted": 0,
            "derived_entities_flagged": 0,
            "derived_unknown_provenance": 0,
            "derived_conflicts_deleted": 0,
            "derived_edges_deleted_uuids": [],
            "derived_edges_flagged_uuids": [],
            "derived_entities_deleted_uuids": [],
            "derived_entities_flagged_uuids": [],
            "touched": False,
            "memory_touched": 0,
            "assertions_deleted": 0,
            "heads_deleted": 0,
            "dependent_views_retired": 0,
            "dependent_views_scrubbed": 0,
            "view_repairs_created": 0,
            "watermarks_reset": 0,
            "event_assertions_deleted": 0,
            "event_heads_deleted": 0,
            "repairs": [],
        }

    def _delete_memory_with_scalar_cascade_in_namespace(
        self,
        node_uuid: str,
        *,
        operation_id: str,
        namespace_key: str,
        erased_uuids: Sequence[str] | None = None,
        deletable_entity_uuids: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """Apply one erasure after locking and revalidating its preflight namespace."""
        erased = list(erased_uuids) if erased_uuids else [node_uuid]
        deletable = None if deletable_entity_uuids is None else list(deletable_entity_uuids)
        # legacy event-history data cleanup, kept after the lane was archived so erasure stays complete
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
                      // An event timeline shows its entries' text; the events below are deleted.
                      OR EXISTS {
                          MATCH (v)-[:EVENT_HISTORY_ENTRY]->(entry:TypedEventAssertion)
                          WHERE entry.episode_uuid = $node_uuid
                             OR entry.turn_evidence_uuid = $node_uuid
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
                           OR w:EventConsolidationWatermark OR w:CountingConsolidationWatermark)
                      AND (coalesce(w.group_id, w.namespace, '') = ns
                           OR (ns = 'default'
                               AND coalesce(w.group_id, w.namespace, '') IN ['', '\\u0000null']))
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
            // Event assertions grounded on the target itself. Left in place they outlive their
            // evidence and the lane's timeline rebuild fails closed on them (#170 item 5). Not
            // via ADMITTED_ON: an admitted turn is erased by its own call, under its own fence.
            CALL {
                OPTIONAL MATCH (event:TypedEventAssertion)
                WHERE event.episode_uuid = $node_uuid
                   OR event.turn_evidence_uuid = $node_uuid
                   OR EXISTS {
                       MATCH (source)-[:FOUNDS|GROUNDS]->(event)
                       WHERE (source:TurnEvidence AND source.turn_id = $node_uuid)
                          OR (source:Episodic AND source.uuid = $node_uuid)
                   }
                WITH collect(DISTINCT event) AS doomed_events
                OPTIONAL MATCH (event_head:TypedEventAssertionHead)
                      -[:HAS_VERSION]->(head_event:TypedEventAssertion)
                WHERE head_event IN doomed_events
                  AND NOT EXISTS {
                      MATCH (event_head)-[:HAS_VERSION]->(outside:TypedEventAssertion)
                      WHERE NOT outside IN doomed_events
                  }
                WITH doomed_events, collect(DISTINCT event_head) AS event_heads
                FOREACH (event IN doomed_events | DETACH DELETE event)
                FOREACH (head IN event_heads | DETACH DELETE head)
                RETURN size(doomed_events) AS event_assertions_deleted,
                       size(event_heads) AS event_heads_deleted
            }
            // Derived edges/entities restating the erased text, before the node's MENTIONS go.
            """ + self._DERIVED_EDGE_CALL + self._DERIVED_ENTITY_CALL + """
            CALL {
                OPTIONAL MATCH (n)
                WHERE ((n:Entity OR n:Episodic) AND n.uuid = $node_uuid)
                   OR (n:TurnEvidence AND n.turn_id = $node_uuid)
                // UNION branches run in order and see earlier branches' writes: branch 2 sets
                // FAILED, so branch 3 must test the state from BEFORE any branch ran or it deletes
                // the in-flight tombstone the worker needs (e2e-21 erase_during_enrichment).
                //
                // A queue node is kept as a content-less tombstone whenever a twin could still
                // exist that this erase does not cover: enrichment running (PENDING/ENRICHING), an
                // unlinked tombstone whose worker may still publish (a repeat erase or replay,
                // review B-2), or a twin linked after the erase resolved its targets (B-1). The
                // late-twin case is flagged so compensation erases the twin.
                WITH n, coalesce(n.processing_state, '') AS prior_state,
                     n.resolved_episode_uuid AS prior_resolved,
                     (n['erased_at'] IS NOT NULL
                      OR coalesce(n.processing_substage, '') = 'deleted_by_user') AS prior_erased
                WITH n, prior_state,
                     (prior_resolved IS NOT NULL AND NOT prior_resolved IN $erased_uuids
                      AND EXISTS { MATCH (lt:Episodic {uuid: prior_resolved}) })
                         AS late_twin,
                     (prior_erased AND prior_resolved IS NULL) AS unlinked_tombstone,
                     (prior_resolved IS NOT NULL AND prior_resolved IN $erased_uuids)
                         AS drop_link
                WITH n, late_twin, drop_link,
                     (prior_state IN ['PENDING', 'ENRICHING'] OR unlinked_tombstone OR late_twin)
                         AS keep_tombstone
                CALL {
                    WITH n, late_twin, keep_tombstone, drop_link
                    WITH n WHERE n:Entity
                    DETACH DELETE n
                    RETURN 1 AS touched
                    UNION
                    WITH n, late_twin, keep_tombstone, drop_link
                    WITH n, late_twin, drop_link WHERE n:Episodic AND keep_tombstone
                    FOREACH (_ IN CASE WHEN drop_link THEN [1] ELSE [] END |
                        REMOVE n.resolved_episode_uuid)
                    SET n.erasure_compensation_pending = CASE
                            WHEN late_twin THEN true ELSE n.erasure_compensation_pending END,
                        n.processing_state = 'FAILED',
                        n.processing_stage = 'failed',
                        n.processing_substage = 'deleted_by_user',
                        n.processing_substage_started_at = datetime(),
                        n.processing_progress = coalesce(n.processing_progress, 100.0),
                        n.processing_completed_at = datetime(),
                        n.processing_owner = null,
                        n.processing_lease_expires_at = null,
                        n.processing_heartbeat_at = datetime(),
                        n.processing_error = 'deleted_by_user',
                        n.erased_at = coalesce(n.erased_at, datetime()),
                        n.processing_llm_active_task = null,
                        n.processing_llm_active_kind = null,
                        n.processing_llm_active_model = null,
                        n.processing_llm_active_endpoint = null
                    // The node is kept so the queue stops; the erased text is not.
                    REMOVE n.content, n.content_embedding, n.content_embedding_model,
                           n.diff, n.ingest_key, n.ingest_request_sha256
                    RETURN 1 AS touched
                    UNION
                    WITH n, late_twin, keep_tombstone
                    WITH n WHERE n:Episodic AND NOT keep_tombstone
                    DETACH DELETE n
                    RETURN 1 AS touched
                    UNION
                    WITH n, late_twin, keep_tombstone
                    WITH n WHERE n:TurnEvidence
                    DETACH DELETE n
                    RETURN 1 AS touched
                }
                RETURN count(touched) AS memory_touched
            }
            WITH repairs, source_keys, assertions_deleted, memory_touched,
                 dependent_views_retired, dependent_views_scrubbed,
                 view_repairs_created, watermarks_reset,
                 event_assertions_deleted, event_heads_deleted,
                 edges_deleted_uuids, edges_flagged_uuids, conflicts_deleted,
                 entities_deleted_uuids, entities_flagged_uuids, derived_unknown_provenance
            UNWIND CASE WHEN size(source_keys) = 0 THEN [null] ELSE source_keys END AS source_key
            OPTIONAL MATCH (h:TypedAssertionHead {source_key: source_key})
            WHERE source_key IS NOT NULL AND NOT (h)-[:HAS_VERSION]->(:TypedAssertion)
            WITH repairs, assertions_deleted, memory_touched, collect(h) AS orphan_heads,
                 dependent_views_retired, dependent_views_scrubbed,
                 view_repairs_created, watermarks_reset,
                 event_assertions_deleted, event_heads_deleted,
                 edges_deleted_uuids, edges_flagged_uuids, conflicts_deleted,
                 entities_deleted_uuids, entities_flagged_uuids, derived_unknown_provenance
            FOREACH (h IN orphan_heads | DETACH DELETE h)
            RETURN assertions_deleted, memory_touched, repairs,
                   size([h IN orphan_heads WHERE h IS NOT NULL]) AS heads_deleted,
                   dependent_views_retired, dependent_views_scrubbed,
                   view_repairs_created, watermarks_reset,
                   event_assertions_deleted, event_heads_deleted,
                   edges_deleted_uuids, edges_flagged_uuids, conflicts_deleted,
                   entities_deleted_uuids, entities_flagged_uuids, derived_unknown_provenance
            """,
            params={
                "node_uuid": node_uuid,
                "operation_id": operation_id,
                "namespace_key": namespace_key,
                "erased_uuids": erased,
                "deletable_entity_uuids": deletable,
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
            "event_assertions_deleted": int(row.get("event_assertions_deleted", 0) or 0),
            "event_heads_deleted": int(row.get("event_heads_deleted", 0) or 0),
            "repairs": [dict(r) for r in (row.get("repairs") or [])],
            "derived_edges_deleted": len(row.get("edges_deleted_uuids") or []),
            "derived_edges_flagged": len(row.get("edges_flagged_uuids") or []),
            "derived_entities_deleted": len(row.get("entities_deleted_uuids") or []),
            "derived_entities_flagged": len(row.get("entities_flagged_uuids") or []),
            "derived_unknown_provenance": int(row.get("derived_unknown_provenance", 0) or 0),
            "derived_conflicts_deleted": int(row.get("conflicts_deleted", 0) or 0),
            "derived_edges_deleted_uuids": list(row.get("edges_deleted_uuids") or []),
            "derived_edges_flagged_uuids": list(row.get("edges_flagged_uuids") or []),
            "derived_entities_deleted_uuids": list(row.get("entities_deleted_uuids") or []),
            "derived_entities_flagged_uuids": list(row.get("entities_flagged_uuids") or []),
        }

    def delete_memory(self, node_uuid: str) -> bool:
        """Compatibility seam that cannot bypass View invalidation or repair journalling."""
        result = self.delete_memory_with_scalar_cascade(
            node_uuid, operation_id=uuid4().hex
        )
        return bool(result["touched"])

    def scrub_merge_audit_for_episodes(
        self,
        erased_uuids: Sequence[str],
        deleted_entity_uuids: Sequence[str] = (),
        *,
        namespace: str | None = None,
    ) -> dict[str, Any]:
        """Tombstone merge_audit entries that copied erased content; such merges become irreversible.

        Survivors are matched inside ``namespace`` only (``None`` is the legacy unscoped
        behaviour). Needles must be canonical uuids; an entry is tombstoned only when one of its
        parsed JSON string leaves EQUALS a needle. An entry that mentions a needle but cannot be
        parsed is left in place and counted in ``merge_audits_unscrubbed``.
        """
        import json

        from menhir.domain.erase_target import canonical_uuid_needles

        needles = canonical_uuid_needles([*erased_uuids, *deleted_entity_uuids])
        result: dict[str, Any] = {
            "merge_audits_tombstoned": 0,
            "merge_absorbed_uuids": [],
            "merge_audits_unscrubbed": 0,
        }
        if not needles:
            return result
        needle_set = set(needles)

        def _leaves(node: Any) -> Iterator[str]:
            if isinstance(node, str):
                yield node
            elif isinstance(node, dict):
                for key, val in node.items():
                    yield str(key)
                    yield from _leaves(val)
            elif isinstance(node, (list, tuple)):
                for val in node:
                    yield from _leaves(val)

        def _classify(entry: str) -> str:
            """'keep' (no needle), 'match' (exact leaf hit) or 'unparseable' (mentions a needle)."""
            try:
                parsed = json.loads(entry)
            except (TypeError, ValueError):
                return "unparseable" if any(u in entry for u in needles) else "keep"
            return "match" if any(leaf in needle_set for leaf in _leaves(parsed)) else "keep"

        # CONTAINS is only a candidate prefilter over validated uuids; the exact decision is made
        # per parsed entry in Python.
        rows = self.neo4j.execute(
            f"""
            MATCH (s:Entity)
            WHERE {tenant_scope_cypher("s")}
              AND any(entry IN coalesce(s.merge_audit, [])
                      WHERE any(u IN $all WHERE entry CONTAINS u))
            RETURN s.uuid AS uuid, s.merge_audit AS merge_audit
            """,
            params={"all": needles, **tenant_scope_params(namespace)},
        )
        absorbed: list[str] = []
        for row in rows or []:
            suuid = row.get("uuid")
            if not suuid:
                continue
            row_unparseable = 0
            for attempt in range(2):
                old = [str(e) for e in (row.get("merge_audit") or [])]
                new: list[str] = []
                tombstoned = 0
                unparseable = 0
                found: list[str] = []
                for entry in old:
                    kind = _classify(entry)
                    if kind == "unparseable":
                        unparseable += 1
                    if kind != "match":
                        new.append(entry)
                        continue
                    try:
                        parsed = json.loads(entry)
                    except (TypeError, ValueError):
                        parsed = None
                    parsed = parsed if isinstance(parsed, dict) else {}
                    absorbed_uuid = parsed.get("absorbed_uuid")
                    if absorbed_uuid:
                        found.append(str(absorbed_uuid))
                    new.append(json.dumps({
                        "erased": True,
                        "absorbed_uuid": absorbed_uuid,
                        "survivor_uuid": parsed.get("survivor_uuid") or suuid,
                        "reason": "source_erased",
                    }))
                    tombstoned += 1
                row_unparseable = unparseable
                if not tombstoned:
                    break
                written = self.neo4j.execute(
                    """
                    MATCH (s:Entity {uuid: $uuid})
                    WHERE s.merge_audit = $old
                    SET s.merge_audit = $new, s.merge_irreversible = true
                    RETURN count(s) AS updated
                    """,
                    params={"uuid": suuid, "old": old, "new": new},
                )
                if written and int(written[0].get("updated", 0) or 0) > 0:
                    result["merge_audits_tombstoned"] += tombstoned
                    absorbed.extend(found)
                    break
                if attempt == 0:
                    # merge_audit changed under us: re-read once and retry.
                    again = self.neo4j.execute(
                        "MATCH (s:Entity {uuid: $uuid}) RETURN s.merge_audit AS merge_audit",
                        params={"uuid": suuid},
                    )
                    row = {"uuid": suuid, "merge_audit": (again[0].get("merge_audit") if again else [])}
                else:
                    logger.warning(
                        "merge_audit scrub lost a compare-and-set twice for %s; entry left in place",
                        suuid,
                    )
                    result["merge_audits_unscrubbed"] += 1
            result["merge_audits_unscrubbed"] += row_unparseable
        result["merge_absorbed_uuids"] = list(dict.fromkeys(absorbed))
        return result

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
        # legacy event-history data cleanup, kept after the lane was archived so erasure stays complete
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
            WHERE """ + NAMESPACE_ERASE_PREDICATE + """
            WITH f, repairs, collect(DISTINCT n) AS direct
            """ + NAMESPACE_SUBORDINATES_CALL + """
            WITH f, repairs, direct + subs AS doomed
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
                       OR w:EventConsolidationWatermark OR w:CountingConsolidationWatermark)
                  AND (coalesce(w.group_id, w.namespace, '') = ns
                       OR (ns = 'default'
                           AND coalesce(w.group_id, w.namespace, '') IN ['', '\\u0000null']))
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
