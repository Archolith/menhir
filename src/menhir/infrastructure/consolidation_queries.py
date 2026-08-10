"""Consolidation, decay, session management, and conflict resolution queries.

Extracted from MemoryGraphAdapter. The adapter delegates all Entity decay,
session promotion, and conflict governance operations here.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from menhir.infrastructure.cypher import Cypher
from menhir.infrastructure.neo4j import Neo4jRepository

logger = logging.getLogger(__name__)


def _content_overlap_ratio(left: str | None, right: str | None) -> float:
    """Compute simple Jaccard overlap ratio over lowercase token sets."""

    left_tokens = set(re.findall(r"[A-Za-z0-9_]+", (left or "").lower()))
    right_tokens = set(re.findall(r"[A-Za-z0-9_]+", (right or "").lower()))
    if not left_tokens and not right_tokens:
        return 1.0
    union = left_tokens | right_tokens
    if not union:
        return 0.0
    intersection = left_tokens & right_tokens
    return len(intersection) / len(union)


@dataclass
class ConsolidationRepository:
    """Encapsulates Entity decay, session promotion, and conflict resolution."""

    neo4j: Neo4jRepository

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

    def increment_edge_weight(self, edge_uuid: str) -> bool:
        """Increment traversal weight for an edge while capping at the v1 max."""

        rows = self.neo4j.execute(
            """
            MATCH ()-[r]-()
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

    def update_edge_facts(self, updates: list[dict[str, str]]) -> int:
        """Bulk-update edge facts and provenance. Returns count updated."""
        if not updates:
            return 0
        rows = self.neo4j.execute(
            """
            UNWIND $updates AS update
            MATCH ()-[r]-()
            WHERE r.uuid = update.uuid
            SET r.fact = update.fact, r.fact_source = update.fact_source
            RETURN count(r) AS updated
            """,
            params={"updates": updates},
        )
        return int(rows[0].get("updated", 0)) if rows else 0

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
                   "coalesce(n.user_flagged, false) = false",
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
            .build())
        params: dict[str, object] = {
            "freshness": freshness,
            "min_days_since_accessed": min_days_since_accessed,
            "max_edge_count": max_edge_count,
        }
        if max_sharpness is not None:
            params["max_sharpness"] = max_sharpness
        return self.neo4j.execute(query, params=params)

    def compress_node(self, node_uuid: str, compressed_summary: str) -> bool:
        """Transition a PERSISTENT ACTIVE node to COMPRESSED with a summary."""

        query = (Cypher()
            .match("(n:Entity {uuid: $node_uuid})")
            .where("n.scope = 'PERSISTENT'", "n.freshness = 'ACTIVE'")
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
            .where("n.scope = 'PERSISTENT'", "n.freshness = 'COMPRESSED'")
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

        rows = self.neo4j.execute(
            """
            MATCH (n:Entity {uuid: $node_uuid})
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
            """,
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
            .where("n.uuid = $node_uuid")
            .set("n.sharpness = $sharpness")
            .return_raw("count(n) AS updated")
            .build())
        rows = self.neo4j.execute(query, params={"node_uuid": node_uuid, "sharpness": sharpness})
        return bool(rows and int(rows[0].get("updated", 0)) > 0)

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
            .where("n.scope = 'SESSION'")
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
            .where("n.uuid IN $uuids", "n.scope = 'SESSION'")
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
            .where("n.uuid IN $uuids", "n.scope = 'SESSION'")
            .detach_delete("n")
            .return_raw("count(n) AS deleted")
            .build())
        rows = self.neo4j.execute(query, params={"uuids": node_uuids})
        return int(rows[0].get("deleted", 0)) if rows else 0

    def delete_entities_returning_uuids(
        self, node_uuids: list[str], *, require_scope: str | None = None
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
        rows = self.neo4j.execute(
            f"""
            MATCH (n:Entity)
            WHERE n.uuid IN $uuids {scope_clause}
            WITH collect(n) AS doomed, collect(n.uuid) AS deleted_uuids
            FOREACH (d IN doomed | DETACH DELETE d)
            RETURN deleted_uuids
            """,
            params=params,
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

        # Count nodes in the set that currently have NO ttl_expires (newly demoted).
        pre_count_rows = self.neo4j.execute(
            """
            MATCH (n:Entity)
            WHERE n.uuid IN $uuids AND n.scope = 'SESSION' AND n.ttl_expires IS NULL
            RETURN count(n) AS newly_demoted_count
            """,
            params={"uuids": node_uuids},
        )
        newly_demoted_count = int(pre_count_rows[0].get("newly_demoted_count", 0)) if pre_count_rows else 0

        # Apply coalesce: only set if not already set.
        query = (Cypher()
            .match("(n:Entity)")
            .where("n.uuid IN $uuids", "n.scope = 'SESSION'")
            .set("n.ttl_expires = coalesce(n.ttl_expires, datetime() + duration({days: $days}))")
            .return_raw("count(n) AS updated")
            .build())
        rows = self.neo4j.execute(query, params={"uuids": node_uuids, "days": ttl_days})
        return newly_demoted_count

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
                   "n.ttl_expires < datetime()")
            .where_if(session_id is not None, "n.session_id = $session_id")
            .return_raw("""n.uuid AS uuid,
       n.name AS name,
       n.session_id AS session_id""")
            .build())
        return self.neo4j.execute(query, params=params)

    # -------------------------------------------------------------------------
    # Conflict governance (M5)
    # -------------------------------------------------------------------------

    def set_conflict(
        self,
        node_uuid_a: str,
        node_uuid_b: str,
        new_group_id: str,
        *,
        initial_status: str = "pending_llm_review",
    ) -> tuple[str, int]:
        """Mark two nodes as conflicting, joining or merging any existing groups.

        The caller passes ``new_group_id`` as a fallback; if either node already
        belongs to a conflict group the existing ID is used as canonical.  When
        both nodes are in *different* existing groups all members of the retired
        group are migrated into the canonical one.

        ``initial_status`` defaults to ``'pending_llm_review'`` so new detections
        wait for LLM confirmation before surfacing to the user as ``'unresolved'``.

        Returns (canonical_group_id, nodes_updated).
        """
        rows = self.neo4j.execute(
            """
            MATCH (a:Entity), (b:Entity)
            WHERE a.uuid = $uuid_a AND b.uuid = $uuid_b
            WITH a, b,
              CASE
                WHEN a.conflict_group_id IS NOT NULL THEN a.conflict_group_id
                WHEN b.conflict_group_id IS NOT NULL THEN b.conflict_group_id
                ELSE $new_group_id
              END AS canonical_group_id,
              CASE
                WHEN a.conflict_group_id IS NOT NULL
                     AND b.conflict_group_id IS NOT NULL
                     AND a.conflict_group_id <> b.conflict_group_id
                THEN b.conflict_group_id
                ELSE null
              END AS retired_group_id
            OPTIONAL MATCH (orphan:Entity)
            WHERE orphan.conflict_group_id = retired_group_id
            WITH a, b, canonical_group_id, collect(orphan) AS orphans
            SET a.conflict_group_id  = canonical_group_id,
                a.conflict_status    = $initial_status,
                a.conflict_created_at = coalesce(a.conflict_created_at, datetime()),
                b.conflict_group_id  = canonical_group_id,
                b.conflict_status    = $initial_status,
                b.conflict_created_at = coalesce(b.conflict_created_at, datetime())
            FOREACH (o IN orphans |
                SET o.conflict_group_id = canonical_group_id,
                    o.conflict_status   = $initial_status
            )
            RETURN canonical_group_id, 2 + size(orphans) AS updated
            """,
            params={
                "uuid_a": node_uuid_a,
                "uuid_b": node_uuid_b,
                "new_group_id": new_group_id,
                "initial_status": initial_status,
            },
        )
        if not rows:
            return new_group_id, 0
        canonical = str(rows[0].get("canonical_group_id") or new_group_id)
        updated = int(rows[0].get("updated", 0))
        return canonical, updated

    def set_conflict_group_status(self, group_id: str, status: str) -> int:
        """Update conflict_status for all members of a group. Returns nodes updated."""
        rows = self.neo4j.execute(
            """
            MATCH (n:Entity)
            WHERE n.conflict_group_id = $group_id
            SET n.conflict_status = $status
            RETURN count(n) AS updated
            """,
            params={"group_id": group_id, "status": status},
        )
        return int(rows[0].get("updated", 0)) if rows else 0

    def requeue_conflicts_for_llm_review(
        self,
        *,
        from_status: str = "unresolved",
        limit: int = 200,
    ) -> int:
        """Re-set conflict_status to pending_llm_review for groups in from_status.

        Returns distinct group count re-queued.
        """
        safe_limit = max(1, min(limit, 1000))
        rows = self.neo4j.execute(
            """
            MATCH (n:Entity)
            WHERE n.conflict_group_id IS NOT NULL
              AND n.conflict_status = $from_status
            WITH DISTINCT n.conflict_group_id AS gid
            LIMIT $limit
            MATCH (m:Entity)
            WHERE m.conflict_group_id = gid
            SET m.conflict_status = 'pending_llm_review'
            RETURN count(DISTINCT gid) AS groups_requeued
            """,
            params={"from_status": from_status, "limit": safe_limit},
        )
        return int(rows[0].get("groups_requeued", 0)) if rows else 0

    def list_conflict_groups(
        self,
        *,
        status: str | None = "unresolved",
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        """Return grouped conflicts keyed by conflict_group_id.

        When ``status`` is ``None`` all conflict statuses are included.
        """
        safe_limit = max(1, min(limit, 200))
        return self.neo4j.execute(
            """
            MATCH (n:Entity)
            WHERE n.conflict_group_id IS NOT NULL
              AND ($status IS NULL OR n.conflict_status = $status)
            WITH n.conflict_group_id AS group_id,
                 min(n.conflict_created_at) AS created_at,
                 collect({
                   uuid: n.uuid,
                   name: n.name,
                   content: coalesce(n.summary, n.content, ''),
                   status: n.conflict_status,
                   scope: n.scope,
                   node_created_at: n.created_at
                 }) AS members
            RETURN
              group_id,
              created_at,
              members
            ORDER BY created_at DESC
            LIMIT $limit
            """,
            params={"status": status, "limit": safe_limit},
        )

    def list_conflict_pairs(
        self,
        *,
        status: str | None = "unresolved",
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        """Compatibility wrapper for older callers.

        Returns grouped rows from ``list_conflict_groups``.
        """
        return self.list_conflict_groups(status=status, limit=limit)

    def bridge_edges_for_node(self, node_uuid: str) -> int:
        """Bridge edges around a single GONE node without deleting it.

        Called immediately after setting ``freshness = 'GONE'`` during manual
        conflict resolution so that the removed node's former neighbors are
        reconnected before the 24 h decay sweep runs.

        Excludes ANCHORED_TO edges and structural neighbors from bridging.
        """
        rows = self.neo4j.execute(
            """
            MATCH (n:Entity {uuid: $node_uuid})
            WHERE n.freshness = 'GONE'
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
            RETURN coalesce(edges_bridged, 0) AS edges_bridged
            """,
            params={"node_uuid": node_uuid},
        )
        return int(rows[0].get("edges_bridged", 0)) if rows else 0

    def bridge_edges_for_nodes(self, node_uuids: list[str]) -> int:
        """Bridge edges around multiple GONE nodes efficiently in one query.

        Called immediately after setting ``freshness = 'GONE'`` during manual
        conflict resolution so that the removed nodes' former neighbors are
        reconnected before the 24 h decay sweep runs.

        Excludes ANCHORED_TO edges and structural neighbors from bridging.
        """
        if not node_uuids:
            return 0

        rows = self.neo4j.execute(
            """
            UNWIND $node_uuids AS node_uuid
            MATCH (n:Entity {uuid: node_uuid})
            WHERE n.freshness = 'GONE'
            CALL {
                WITH n, node_uuid
                OPTIONAL MATCH (n)-[r]-(neighbor:Entity)
                WHERE NOT type(r) = 'ANCHORED_TO'
                  AND neighbor.structure_role IS NULL
                WITH node_uuid, collect(DISTINCT neighbor) AS neighbors
                UNWIND neighbors AS a
                UNWIND neighbors AS b
                WITH node_uuid, a, b WHERE a.uuid < b.uuid
                MERGE (a)-[r:RELATES_TO]->(b)
                ON CREATE SET r.type = 'bridged',
                              r.bridged_from = node_uuid,
                              r.weight = 1.0,
                              r.source = 'system-derived',
                              r.scope = 'PERSISTENT',
                              r.created_at = datetime(),
                              r.last_traversed = datetime()
                RETURN count(*) AS edges_bridged
            }
            RETURN sum(coalesce(edges_bridged, 0)) AS total_edges_bridged
            """,
            params={"node_uuids": node_uuids},
        )
        return int(rows[0].get("total_edges_bridged", 0)) if rows else 0

    def resolve_conflict_group(
        self,
        conflict_group_id: str,
        action: str,
        *,
        keep_uuid: str | None = None,
        remove_uuid: str | None = None,
        resolution_status: str = "resolved",
        allow_promoted_removal: bool = False,
    ) -> dict[str, Any]:
        """Resolve a conflict group via one of three actions.

        Actions:
        - ``keep_both``    — both nodes are valid; clear group, set resolved on all.
        - ``replace``      — ``remove_uuid`` is superseded; content absorbed into
                             ``keep_uuid``, removed node set to GONE, edges bridged.
        - ``discard_new``  — same mechanics as ``replace``.

        ``resolution_status`` defaults to ``'resolved'``; pass ``'auto-resolved'``
        for the scheduler auto-resolve path (single write, no intermediate state).

        Returns a summary dict with action, group_id, resolved count, and
        remove_uuid when applicable.
        """
        _VALID_ACTIONS = {"keep_both", "replace", "discard_new"}
        if action not in _VALID_ACTIONS:
            raise ValueError(f"Invalid action {action!r}; must be one of {_VALID_ACTIONS}")

        # Prefetch member UUIDs before any mutation (needed for suppression recording)
        prefetch_rows = self.neo4j.execute(
            (Cypher()
                .match("(n:Entity)")
                .where("n.conflict_group_id = $group_id")
                .return_raw("n.uuid AS uuid")
                .build()),
            params={"group_id": conflict_group_id},
        )
        prefetched_member_uuids = [str(r["uuid"]) for r in prefetch_rows if r.get("uuid")]

        if action == "keep_both":
            rows = self.neo4j.execute(
                (Cypher()
                    .match("(n:Entity)")
                    .where("n.conflict_group_id = $group_id")
                    .set(("n.conflict_status = $resolution_status", "n.conflict_group_id = null"))
                    .return_raw("count(n) AS resolved")
                    .build()),
                params={"group_id": conflict_group_id, "resolution_status": resolution_status},
            )
            resolved = int(rows[0].get("resolved", 0)) if rows else 0
            return {
                "action": action,
                "group_id": conflict_group_id,
                "resolved": resolved,
                "removed_uuid": None,
                "removed_uuids": [],
                "bridged_edges": 0,
                "member_uuids": prefetched_member_uuids,
            }

        if not keep_uuid:
            raise ValueError(f"action={action!r} requires keep_uuid")

        members = self.neo4j.execute(
            (Cypher()
                .match("(n:Entity)")
                .where("n.conflict_group_id = $group_id")
                .return_raw("n.uuid AS uuid, n.scope AS scope, n.name AS name")
                .build()),
            params={"group_id": conflict_group_id},
        )
        member_uuids = {str(row.get("uuid") or "") for row in members if row.get("uuid")}
        if keep_uuid not in member_uuids:
            raise ValueError(f"keep_uuid {keep_uuid!r} is not a member of conflict_group_id={conflict_group_id!r}")

        if remove_uuid is None:
            remove_uuids = sorted(uuid for uuid in member_uuids if uuid != keep_uuid)
        else:
            if remove_uuid == keep_uuid:
                raise ValueError("remove_uuid cannot equal keep_uuid")
            if remove_uuid not in member_uuids:
                raise ValueError(
                    f"remove_uuid {remove_uuid!r} is not a member of conflict_group_id={conflict_group_id!r}"
                )
            remove_uuids = [remove_uuid]

        if not remove_uuids:
            raise ValueError("No removable members found in conflict group")

        if not allow_promoted_removal:
            promoted = sorted(
                (
                    str(row.get("name") or "(unnamed)"),
                    str(row.get("uuid") or ""),
                )
                for row in members
                if str(row.get("uuid") or "") in set(remove_uuids) and str(row.get("scope") or "") == "PROMOTED"
            )
            if promoted:
                raise ValueError(
                    "Refusing to GONE PROMOTED node(s): "
                    + ", ".join(f"{name} ({uuid})" for name, uuid in promoted)
                    + ". Pass allow_promoted_removal=True to override."
                )

        keep_rows = self.neo4j.execute(
            (Cypher()
                .match("(n:Entity)")
                .where("n.uuid = $uuid")
                .return_raw("n.content AS content, n.original_content AS original_content")
                .build()),
            params={"uuid": keep_uuid},
        )
        keep_content: str = (keep_rows[0].get("content") or "") if keep_rows else ""

        # --- Content absorption before GONE ---
        for target_uuid in remove_uuids:
            remove_rows = self.neo4j.execute(
                (Cypher()
                    .match("(n:Entity)")
                    .where("n.uuid = $uuid")
                    .return_raw("n.content AS content")
                    .build()),
                params={"uuid": target_uuid},
            )
            remove_content: str = (remove_rows[0].get("content") or "") if remove_rows else ""

            if remove_content.strip():
                if action == "discard_new":
                    overlap = _content_overlap_ratio(keep_content, remove_content)
                    if overlap >= 0.85:
                        logger.debug(
                            "Skipping absorption for discard_new due to high overlap keep=%s remove=%s ratio=%.3f",
                            keep_uuid,
                            target_uuid,
                            overlap,
                        )
                        continue
                absorbed_date = datetime.now(timezone.utc).date().isoformat()
                merged = (
                    f"{keep_content}\n\n"
                    f"--- Absorbed from superseded memory ({target_uuid}) on {absorbed_date} ---\n"
                    f"{remove_content}"
                )
                self.neo4j.execute(
                    (Cypher()
                        .match("(n:Entity)")
                        .where("n.uuid = $uuid")
                        .set(("n.content = $content",
                              "n.original_content = coalesce(n.original_content, $original)"))
                        .build()),
                    params={"uuid": keep_uuid, "content": merged, "original": keep_content},
                )
                logger.debug(
                    "Content absorbed from %s into %s (%d chars appended)",
                    target_uuid,
                    keep_uuid,
                    len(remove_content),
                )
                keep_content = merged

        # --- Step 1: GONE selected members ---
        gone_rows = self.neo4j.execute(
            (Cypher()
                .match("(n:Entity)")
                .where("n.conflict_group_id = $group_id", "n.uuid IN $remove_uuids")
                .set(("n.freshness = 'GONE'",
                      "n.conflict_status = $resolution_status",
                      "n.conflict_group_id = null"))
                .return_raw("collect(n.uuid) AS removed_uuids")
                .build()),
            params={
                "group_id": conflict_group_id,
                "remove_uuids": remove_uuids,
                "resolution_status": resolution_status,
            },
        )
        removed_uuids = []
        if gone_rows:
            removed_uuids = [str(uuid) for uuid in (gone_rows[0].get("removed_uuids") or []) if str(uuid)]
        if not removed_uuids:
            removed_uuids = remove_uuids

        # --- Step 2: resolve all remaining group members ---
        rows = self.neo4j.execute(
            (Cypher()
                .match("(n:Entity)")
                .where("n.conflict_group_id = $group_id")
                .set(("n.conflict_status = $resolution_status", "n.conflict_group_id = null"))
                .return_raw("count(n) AS resolved")
                .build()),
            params={"group_id": conflict_group_id, "resolution_status": resolution_status},
        )
        resolved = int(rows[0].get("resolved", 0)) if rows else 0

        if action == "discard_new":
            self.neo4j.execute(
                (Cypher()
                    .match("(n:Entity)")
                    .where("n.uuid = $keep_uuid")
                    .set(("n.sharpness = CASE"
                          " WHEN n.sharpness IS NULL THEN 0.0"
                          " WHEN toFloat(n.sharpness) - 0.1 < 0.0 THEN 0.0"
                          " ELSE toFloat(n.sharpness) - 0.1 END",
                          "n.pending_review = true"))
                    .build()),
                params={"keep_uuid": keep_uuid},
            )

        # --- Immediate edge bridge (avoid 24 h dead zone) ---
        bridged_total = self.bridge_edges_for_nodes(removed_uuids)

        return {
            "action": action,
            "group_id": conflict_group_id,
            "resolved": resolved,
            "removed_uuid": removed_uuids[0] if removed_uuids else None,
            "removed_uuids": removed_uuids,
            "bridged_edges": bridged_total,
            "member_uuids": prefetched_member_uuids,
        }
