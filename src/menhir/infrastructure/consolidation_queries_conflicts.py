"""Conflict group governance: status updates, requeue, and listing (M5).

Moved verbatim from ``consolidation_queries.py`` and composed into
``ConsolidationRepository`` via this mixin. ``set_conflict``, the only writer
of ``conflict_group_id``, stays on the facade class itself.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from menhir.domain.namespace import tenant_scope_cypher, tenant_scope_params
from menhir.infrastructure.consolidation_queries_common import (
    automatic_lifecycle_protection_cypher,
)


class ConsolidationConflictGovernanceMixin:
    """Conflict-group status, requeue, and listing operations for ``ConsolidationRepository``."""

    # -------------------------------------------------------------------------
    # Conflict governance (M5)
    # -------------------------------------------------------------------------

    def set_conflict_group_status(self, group_id: str, status: str) -> int:
        """Update conflict_status for all members of a group. Returns nodes updated."""
        rows = self.neo4j.execute(
            f"""
            MATCH (n:Entity)
            WHERE n.conflict_group_id = $group_id
              AND {automatic_lifecycle_protection_cypher("n")}
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
        namespace: str | None = None,
    ) -> int:
        """Re-set conflict_status to pending_llm_review for groups in from_status.

        Returns distinct group count re-queued.

        ``namespace`` is opt-in and filters BOTH halves of this statement. Filtering only
        the selecting match would pick groups by the caller's silo and then mutate every
        member of them, including members in another silo if a legacy group is mixed --
        a cross-tenant write authorized by a same-tenant read. The predicate on ``m`` is
        what makes the write set a subset of what the caller may see.
        """
        safe_limit = max(1, min(limit, 1000))
        ns = str(namespace).strip() if namespace is not None else ""
        rows = self.neo4j.execute(
            f"""
            MATCH (n:Entity)
            WHERE n.conflict_group_id IS NOT NULL
              AND n.conflict_status = $from_status
              AND ($namespace IS NULL OR coalesce(n.namespace, 'default') = $namespace)
              AND {automatic_lifecycle_protection_cypher("n")}
            WITH DISTINCT n.conflict_group_id AS gid
            LIMIT $limit
            MATCH (m:Entity)
            WHERE m.conflict_group_id = gid
              AND ($namespace IS NULL OR coalesce(m.namespace, 'default') = $namespace)
              AND {automatic_lifecycle_protection_cypher("m")}
            SET m.conflict_status = 'pending_llm_review'
            RETURN count(DISTINCT gid) AS groups_requeued
            """,
            params={
                "from_status": from_status,
                "limit": safe_limit,
                "namespace": ns or None,
            },
        )
        return int(rows[0].get("groups_requeued", 0)) if rows else 0

    def list_conflict_groups(
        self,
        *,
        status: str | None = "unresolved",
        limit: int = 25,
        namespace: str | None = None,
        created_before: datetime | None = None,
        oldest_first: bool = False,
    ) -> list[dict[str, Any]]:
        """Return grouped conflicts keyed by conflict_group_id.

        When ``status`` is ``None`` all conflict statuses are included.

        ``namespace`` filters members to one silo. It is opt-in per the namespace
        contract: ``None``/empty does not filter, preserving today's behavior exactly.

        ``created_before`` (a cutoff datetime) and ``oldest_first`` (bool) are opt-in for
        the stale-auto-resolver (CF-120) only. When both are left at their defaults the
        emitted Cypher and params are byte-for-byte identical to the pre-CF-120 form, so
        the MCP/explorer callers keep their newest-first listing.

        The predicate is applied per MEMBER, not per group. Conflict groups are
        namespace-homogeneous by construction -- the only writer of
        ``conflict_group_id`` is ``set_conflict``, whose single caller
        (``lifecycle_consolidation._check_contradictions_batch``) searches for pair
        candidates with ``group_ids=namespace_to_group_ids(node.namespace)``, so a pair
        can only form inside one silo, and the group-merge branch of ``set_conflict``
        preserves that inductively. A member-level predicate is nonetheless the correct
        form: it is what makes a legacy group that predates that scoping return only the
        caller's own half rather than leaking the other silo's content.
        """
        safe_limit = max(1, min(limit, 200))
        ns = str(namespace).strip() if namespace is not None else ""
        params: dict[str, Any] = {
            "status": status,
            "limit": safe_limit,
            **tenant_scope_params(ns or None),
        }

        # Filtered POST-aggregation. A group's age is min(conflict_created_at) over its members,
        # so a member-level predicate would change what min() sees and could age a group by a
        # member the filter excluded. The trade is that the conflict_created_at index cannot
        # serve a post-aggregation filter; correctness of the age wins over the index here.
        cutoff_clause = ""
        if created_before is not None:
            params["created_before"] = created_before
            cutoff_clause = "\n            WHERE created_at < $created_before"

        # Both fragments below are constants chosen here, never caller text -- Cypher cannot
        # parameterize a sort direction, so this stays interpolation and stays a closed set.
        order = "ASC" if oldest_first else "DESC"

        return self.neo4j.execute(
            f"""
            MATCH (n:Entity)
            WHERE n.conflict_group_id IS NOT NULL
              AND ($status IS NULL OR n.conflict_status = $status)
              AND {tenant_scope_cypher("n")}
              AND {automatic_lifecycle_protection_cypher("n")}
            WITH n.conflict_group_id AS group_id,
                 min(n.conflict_created_at) AS created_at,
                 collect({{
                   uuid: n.uuid,
                   name: n.name,
                   content: coalesce(n.summary, n.content, ''),
                   status: n.conflict_status,
                   scope: n.scope,
                   node_created_at: n.created_at
                 }}) AS members{cutoff_clause}
            RETURN
              group_id,
              created_at,
              members
            ORDER BY created_at {order}
            LIMIT $limit
            """,
            params=params,
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
