"""Namespace management and memory-query delegates for the memory graph adapter.

Methods moved verbatim from ``memory_graph_adapter.py`` (facade split); this
mixin is composed into :class:`~menhir.infrastructure.memory_graph_adapter.MemoryGraphAdapter`.
"""

from __future__ import annotations

import logging
import uuid as uuidlib
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


class MemoryGraphMemoryQueriesMixin:
    """Namespace management and memory-query delegates (mixin for MemoryGraphAdapter)."""

    def count_namespace(self, group_id: str, *, namespace: str | None = None) -> int:
        """Count nodes in the given graphiti group partition, without deleting anything.

        :TurnEvidence is included here for the same reason it is included in the delete and in
        the pre-erasure capture: these three predicates must name the same set. A dry_run that
        under-reported the blast radius, a capture that missed a subject, and a delete that left
        the node behind were all the same omission.

        Used by the delete_namespace safety gate to report the blast radius before an
        irreversible DETACH DELETE.

        In addition to the ``group_id`` partition (which already covers the namespace-keyed
        :EventConsolidationWatermark cursor) and the scalar/episode namespace rows, counts the
        durable event log: every :TypedEventAssertion in the namespace and every
        :TypedEventAssertionHead that HAS_VERSION to an event assertion in the namespace.
        """
        namespace_clause = (
            " OR (n:Episodic AND n.namespace = $namespace)"
            " OR ((n:TypedAssertion OR n:TypedAssertionHead OR "
            "n:ScalarConsolidationWatermark OR n:TurnEvidence) AND n.namespace = $namespace)"
            " OR (n:TypedEventAssertion AND n.namespace = $namespace)"
            " OR (n:TypedEventAssertionHead AND EXISTS {"
            " MATCH (n)-[:HAS_VERSION]->(ev:TypedEventAssertion) WHERE ev.namespace = $namespace})"
            if namespace is not None else ""
        )
        rows = self.neo4j.execute(
            f"MATCH (n) WHERE n.group_id = $group_id{namespace_clause} "
            "RETURN count(DISTINCT n) AS total",
            params={"group_id": group_id, "namespace": namespace},
        )
        return int(rows[0].get("total", 0)) if rows else 0

    def fetch_node_namespaces(self, uuids: list[str]) -> dict[str, str]:
        """Map uuid -> namespace for nodes that still exist. Missing uuids are absent.

        Used by the CF-165 lineage backfill. Absence is the load-bearing part: a uuid with no
        row here has no provable namespace, and the backfill must leave it NULL rather than
        guess one.
        """
        wanted = [str(u) for u in uuids if u]
        if not wanted:
            return {}
        rows = self.neo4j.execute(
            "MATCH (n) WHERE n.uuid IN $uuids "
            "RETURN n.uuid AS uuid, "
            "coalesce(n.namespace, n.group_id) AS namespace",
            params={"uuids": wanted},
        )
        return {
            str(r["uuid"]): str(r["namespace"])
            for r in (rows or [])
            if r.get("uuid") and r.get("namespace")
        }

    def capture_namespace_uuids(
        self, group_id: str, *, namespace: str | None = None
    ) -> list[str]:
        """Return the uuids in a namespace partition, for capture BEFORE erasure (CF-165).

        Deliberately mirrors ``count_namespace``'s predicate rather than inventing its own: the
        captured set is what a sidecar purge will be keyed on, so it must cover exactly what
        ``delete_namespace`` is about to destroy. If the two predicates drifted, an erasure
        would delete graph nodes whose sidecar content it never recorded a subject for.
        """
        namespace_clause = (
            " OR (n:Episodic AND n.namespace = $namespace)"
            " OR ((n:TypedAssertion OR n:TypedAssertionHead OR "
            "n:ScalarConsolidationWatermark OR n:TurnEvidence) AND n.namespace = $namespace)"
            " OR (n:TypedEventAssertion AND n.namespace = $namespace)"
            " OR (n:TypedEventAssertionHead AND EXISTS {"
            " MATCH (n)-[:HAS_VERSION]->(ev:TypedEventAssertion) WHERE ev.namespace = $namespace})"
            if namespace is not None else ""
        )
        rows = self.neo4j.execute(
            f"MATCH (n) WHERE n.group_id = $group_id{namespace_clause} "
            "AND n.uuid IS NOT NULL RETURN DISTINCT n.uuid AS uuid",
            params={"group_id": group_id, "namespace": namespace},
        )
        return [str(r.get("uuid")) for r in (rows or []) if r.get("uuid")]

    def delete_namespace(self, group_id: str, *, namespace: str | None = None) -> int:
        """Delete every node in the given graphiti group partition; returns the count.

        The caller is responsible for refusing the default/shared group and for any
        node-count safety gate. Intended for tearing down throwaway/eval namespaces.
        """
        operation_id = uuidlib.uuid4().hex
        logical_namespace = str(namespace or group_id).strip()
        result = self._memory_queries.delete_namespace_with_scalar_cascade(
            group_id, logical_namespace, operation_id=operation_id)
        repair = self.scalar_state_service().repair_pending_deletions(
            operation_id=operation_id, as_of=datetime.now(timezone.utc))
        if repair["failed"]:
            logger.warning(
                "namespace %s deleted with %d scalar projection repair(s) pending retry",
                namespace, len(repair["failed"]),
            )
        return int(result["deleted"])

    def flag_memory(
        self, node_uuid: str, bootstrap_scope: str | None = None
    ) -> bool:
        """Persist the explicit v1 retention override on a node."""
        return self._memory_queries.flag_memory(
            node_uuid, bootstrap_scope=bootstrap_scope
        )

    def unflag_memory(self, node_uuid: str) -> bool:
        """Remove the explicit user retention override from a node."""
        return self._memory_queries.unflag_memory(node_uuid)

    def promote_memory(self, node_uuid: str) -> bool:
        """Promote a PERSISTENT memory to PROMOTED (operator-curated ground truth, SSOT-08)."""
        return self._memory_queries.promote_memory(node_uuid)

    def delete_memory(self, node_uuid: str) -> bool:
        """Delete a memory/observation, cascade its assertions, and repair scalar projections."""
        operation_id = uuidlib.uuid4().hex
        result = self._memory_queries.delete_memory_with_scalar_cascade(
            node_uuid, operation_id=operation_id)
        repair = self.scalar_state_service().repair_pending_deletions(
            operation_id=operation_id, as_of=datetime.now(timezone.utc))
        if repair["failed"]:
            logger.warning(
                "memory %s deleted with %d scalar projection repair(s) pending retry",
                node_uuid, len(repair["failed"]),
            )
        return bool(result["touched"])

    def fetch_candidate_metadata(
        self, node_uuids: list[str]
    ) -> list[dict[str, object]]:
        """Fetch scoring-relevant fields for candidate nodes."""
        return self._memory_queries.fetch_candidate_metadata(node_uuids)

    def search_content_embeddings(
        self,
        query_vector: list[float],
        *,
        limit: int = 50,
        group_ids: list[str] | None = None,
    ) -> list[dict[str, object]]:
        return self._memory_queries.search_content_embeddings(
            query_vector, limit=limit, group_ids=group_ids
        )

    def search_assertion_embeddings(
        self,
        query_vector: list[float],
        *,
        limit: int = 50,
        namespaces: list[str] | None = None,
    ) -> list[dict[str, object]]:
        """Observation-lane candidate search over :TypedAssertion embeddings (Phase 4a.2)."""
        return self._memory_queries.search_assertion_embeddings(
            query_vector, limit=limit, namespaces=namespaces
        )

    def fetch_assertion_candidate_metadata(
        self, assertion_ids: list[str]
    ) -> list[dict[str, object]]:
        """Hydrate surfaced :TypedAssertion observations by id (Phase 4a.2 observation lane)."""
        return self._memory_queries.fetch_assertion_candidate_metadata(assertion_ids)

    def fetch_temporal_facts(
        self, node_uuids: list[str]
    ) -> list[dict[str, object]]:
        """Fetch bi-temporal fact-edge state for candidate nodes."""
        return self._memory_queries.fetch_temporal_facts(node_uuids)

    def fetch_candidate_fact_edges(
        self, node_uuids: list[str]
    ) -> list[dict[str, object]]:
        """Fetch fact-edge identity (edge uuid + both endpoints) for candidate nodes."""
        return self._memory_queries.fetch_candidate_fact_edges(node_uuids)

    def fetch_candidate_provenance(
        self, node_uuids: list[str]
    ) -> list[dict[str, object]]:
        """Raw per-candidate provenance (SUPPORTED_BY / ANCHORED_TO project / MENTIONS source)."""
        return self._memory_queries.fetch_candidate_provenance(node_uuids)

    def fetch_node_receipts(self, node_uuid: str) -> dict[str, object] | None:
        """Receipts for one node: its MENTIONS source episodes + SUPPORTED_BY evidence + ANCHORED_TO paths."""
        return self._memory_queries.fetch_node_receipts(node_uuid)

    def fetch_adjacency_pairs(
        self,
        candidate_uuids: list[str],
        context_uuids: list[str] | None = None,
        namespace: str | None = None,
    ) -> list[dict[str, object]]:
        """Find edges connecting candidates to each other or to context nodes."""
        return self._memory_queries.fetch_adjacency_pairs(
            candidate_uuids, context_uuids, namespace=namespace,
        )

    def touch_retrieved_nodes(self, node_uuids: list[str]) -> int:
        """Update last_accessed for retrieved nodes, return count touched."""
        return self._memory_queries.touch_retrieved_nodes(node_uuids)
