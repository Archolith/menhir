"""Conflict resolution mechanics: neighbor bridging and group resolution actions.

Moved verbatim from ``consolidation_queries.py`` and composed into
``ConsolidationRepository`` via this mixin.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from menhir.domain.namespace import tenant_scope_cypher, tenant_scope_params
from menhir.infrastructure.consolidation_queries_common import (
    _content_overlap_ratio,
    automatic_lifecycle_protection_cypher,
)
from menhir.infrastructure.cypher import Cypher

logger = logging.getLogger(__name__)


class ConsolidationConflictResolutionMixin:
    """GONE-node edge bridging and conflict-group resolution for ``ConsolidationRepository``."""

    def bridge_edges_for_node(
        self,
        node_uuid: str,
        *,
        namespace: str | None = None,
    ) -> int:
        """Bridge edges around a single GONE node without deleting it.

        Called immediately after setting ``freshness = 'GONE'`` during manual
        conflict resolution so that the removed node's former neighbors are
        reconnected before the 24 h decay sweep runs.

        Excludes ANCHORED_TO edges and structural neighbors from bridging. When
        ``namespace`` is supplied, the removed node and every bridged neighbor
        are restricted to that silo.
        """
        return self.bridge_edges_for_nodes(
            [node_uuid],
            namespace=namespace,
        )

    def bridge_edges_for_nodes(
        self,
        node_uuids: list[str],
        *,
        namespace: str | None = None,
    ) -> int:
        """Bridge edges around multiple GONE nodes efficiently in one query.

        Called immediately after setting ``freshness = 'GONE'`` during manual
        conflict resolution so that the removed nodes' former neighbors are
        reconnected before the 24 h decay sweep runs.

        Excludes ANCHORED_TO edges and structural neighbors from bridging. When
        ``namespace`` is supplied, the removed nodes and every bridged neighbor
        are restricted to that silo at the final relationship write.
        """
        if not node_uuids:
            return 0

        query = """
            UNWIND $node_uuids AS node_uuid
            MATCH (n:Entity {uuid: node_uuid})
            WHERE n.freshness = 'GONE'
              AND __TENANT_NODE_SCOPE__
              AND __AUTOMATIC_LIFECYCLE_PROTECTION__
            CALL {
                WITH n, node_uuid
                OPTIONAL MATCH (n)-[r]-(neighbor:Entity)
                WHERE NOT type(r) = 'ANCHORED_TO'
                  AND neighbor.structure_role IS NULL
                  AND __TENANT_NEIGHBOR_SCOPE__
                WITH node_uuid, collect(DISTINCT neighbor) AS neighbors
                UNWIND neighbors AS a
                UNWIND neighbors AS b
                WITH node_uuid, a, b
                WHERE a.uuid < b.uuid
                  AND __TENANT_A_SCOPE__
                  AND __TENANT_B_SCOPE__
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
            """.replace(
                "__AUTOMATIC_LIFECYCLE_PROTECTION__",
                automatic_lifecycle_protection_cypher("n"),
            ).replace(
                "__TENANT_NODE_SCOPE__",
                tenant_scope_cypher("n"),
            ).replace(
                "__TENANT_NEIGHBOR_SCOPE__",
                tenant_scope_cypher("neighbor"),
            ).replace(
                "__TENANT_A_SCOPE__",
                tenant_scope_cypher("a"),
            ).replace(
                "__TENANT_B_SCOPE__",
                tenant_scope_cypher("b"),
            )
        rows = self.neo4j.execute(
            query,
            params={"node_uuids": node_uuids, **tenant_scope_params(namespace)},
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
        namespace: str | None = None,
    ) -> dict[str, Any]:
        """Resolve a conflict group via one of three actions.

        Actions:
        - ``keep_both``    — both nodes are valid; clear group, set resolved on all.
        - ``replace``      — ``remove_uuid`` is superseded; content absorbed into
                             ``keep_uuid``, removed node set to GONE, edges bridged.
        - ``discard_new``  — same mechanics as ``replace``.

        ``resolution_status`` defaults to ``'resolved'``; pass ``'auto-resolved'``
        for the scheduler auto-resolve path (single write, no intermediate state).

        ``namespace`` is opt-in. When supplied, every read and mutation is restricted
        to members in that silo. The final write predicates are required even though
        current writers create homogeneous groups: legacy data may contain mixed groups.

        Returns a summary dict with action, group_id, resolved count, and
        remove_uuid when applicable.
        """
        _VALID_ACTIONS = {"keep_both", "replace", "discard_new"}
        if action not in _VALID_ACTIONS:
            raise ValueError(f"Invalid action {action!r}; must be one of {_VALID_ACTIONS}")

        ns = str(namespace).strip() if namespace is not None else ""
        namespace_value = ns or None
        namespace_predicate = tenant_scope_cypher("n")
        namespace_params = tenant_scope_params(namespace_value)

        # Prefetch member UUIDs before any mutation (needed for suppression recording)
        prefetch_rows = self.neo4j.execute(
            (Cypher()
                .match("(n:Entity)")
                .where("n.conflict_group_id = $group_id",
                       namespace_predicate,
                       automatic_lifecycle_protection_cypher("n"))
                .return_raw("n.uuid AS uuid")
                .build()),
            params={"group_id": conflict_group_id, **namespace_params},
        )
        prefetched_member_uuids = sorted(
            str(r["uuid"]) for r in prefetch_rows if r.get("uuid")
        )

        if action == "keep_both":
            rows = self.neo4j.execute(
                (Cypher()
                    .match("(n:Entity)")
                    .where("n.conflict_group_id = $group_id",
                           namespace_predicate,
                           automatic_lifecycle_protection_cypher("n"))
                    .set(("n.conflict_status = $resolution_status", "n.conflict_group_id = null"))
                    .return_raw("count(n) AS resolved")
                    .build()),
                params={
                    "group_id": conflict_group_id,
                    "resolution_status": resolution_status,
                    **namespace_params,
                },
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
                .where("n.conflict_group_id = $group_id",
                       namespace_predicate,
                       automatic_lifecycle_protection_cypher("n"))
                .return_raw("n.uuid AS uuid, n.scope AS scope, n.name AS name")
                .build()),
            params={"group_id": conflict_group_id, **namespace_params},
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
                .where("n.uuid = $uuid", namespace_predicate,
                       automatic_lifecycle_protection_cypher("n"))
                .return_raw("n.content AS content, n.original_content AS original_content")
                .build()),
            params={"uuid": keep_uuid, **namespace_params},
        )
        keep_content: str = (keep_rows[0].get("content") or "") if keep_rows else ""

        # --- Content absorption before GONE ---
        for target_uuid in remove_uuids:
            remove_rows = self.neo4j.execute(
                (Cypher()
                    .match("(n:Entity)")
                    .where("n.uuid = $uuid", namespace_predicate,
                           automatic_lifecycle_protection_cypher("n"))
                    .return_raw("n.content AS content")
                    .build()),
                params={"uuid": target_uuid, **namespace_params},
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
                        .where("n.uuid = $uuid", namespace_predicate,
                               automatic_lifecycle_protection_cypher("n"))
                        .set(("n.content = $content",
                              "n.original_content = coalesce(n.original_content, $original)"))
                        .build()),
                    params={
                        "uuid": keep_uuid,
                        "content": merged,
                        "original": keep_content,
                        **namespace_params,
                    },
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
                .where("n.conflict_group_id = $group_id", "n.uuid IN $remove_uuids",
                       namespace_predicate,
                       automatic_lifecycle_protection_cypher("n"))
                .set(("n.freshness = 'GONE'",
                      "n.conflict_status = $resolution_status",
                      "n.conflict_group_id = null"))
                .return_raw("collect(n.uuid) AS removed_uuids")
                .build()),
            params={
                "group_id": conflict_group_id,
                "remove_uuids": remove_uuids,
                "resolution_status": resolution_status,
                **namespace_params,
            },
        )
        removed_uuids = []
        if gone_rows:
            removed_uuids = [str(uuid) for uuid in (gone_rows[0].get("removed_uuids") or []) if str(uuid)]

        # --- Step 2: resolve all remaining group members ---
        rows = self.neo4j.execute(
            (Cypher()
                .match("(n:Entity)")
                .where("n.conflict_group_id = $group_id",
                       namespace_predicate,
                       automatic_lifecycle_protection_cypher("n"))
                .set(("n.conflict_status = $resolution_status", "n.conflict_group_id = null"))
                .return_raw("count(n) AS resolved")
                .build()),
            params={
                "group_id": conflict_group_id,
                "resolution_status": resolution_status,
                **namespace_params,
            },
        )
        resolved = int(rows[0].get("resolved", 0)) if rows else 0

        if action == "discard_new":
            self.neo4j.execute(
                (Cypher()
                    .match("(n:Entity)")
                    .where("n.uuid = $keep_uuid", namespace_predicate,
                           automatic_lifecycle_protection_cypher("n"))
                    .set(("n.sharpness = CASE"
                          " WHEN n.sharpness IS NULL THEN 0.0"
                          " WHEN toFloat(n.sharpness) - 0.1 < 0.0 THEN 0.0"
                          " ELSE toFloat(n.sharpness) - 0.1 END",
                          "n.pending_review = true"))
                    .build()),
                params={"keep_uuid": keep_uuid, **namespace_params},
            )

        # --- Immediate edge bridge (avoid 24 h dead zone) ---
        bridged_total = self.bridge_edges_for_nodes(
            removed_uuids,
            namespace=namespace_value,
        )

        return {
            "action": action,
            "group_id": conflict_group_id,
            "resolved": resolved,
            "removed_uuid": removed_uuids[0] if removed_uuids else None,
            "removed_uuids": removed_uuids,
            "bridged_edges": bridged_total,
            "member_uuids": prefetched_member_uuids,
        }
