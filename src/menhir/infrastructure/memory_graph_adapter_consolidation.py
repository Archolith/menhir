"""Consolidation and conflict-governance delegates for the memory graph adapter.

Methods moved verbatim from ``memory_graph_adapter.py`` (facade split); this
mixin is composed into :class:`~menhir.infrastructure.memory_graph_adapter.MemoryGraphAdapter`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any


class MemoryGraphConsolidationMixin:
    """Consolidation and conflict-governance delegates (mixin for MemoryGraphAdapter)."""

    # -------------------------------------------------------------------------
    # Consolidation delegates → ConsolidationRepository
    # -------------------------------------------------------------------------

    def fetch_decay_candidates(
        self,
        freshness: str,
        *,
        min_days_since_accessed: float,
        max_edge_count: int,
        max_sharpness: float | None = None,
    ) -> list[dict[str, object]]:
        return self._consolidation.fetch_decay_candidates(
            freshness,
            min_days_since_accessed=min_days_since_accessed,
            max_edge_count=max_edge_count,
            max_sharpness=max_sharpness,
        )

    def compress_node(self, node_uuid: str, compressed_summary: str) -> bool:
        return self._consolidation.compress_node(node_uuid, compressed_summary)

    def fetch_node_freshness(self, node_uuids: list[str]) -> dict[str, str]:
        return self._consolidation.fetch_node_freshness(node_uuids)

    def complete_rehydration(
        self, node_uuid: str, updated_content: str | None = None
    ) -> bool:
        return self._consolidation.complete_rehydration(node_uuid, updated_content)

    def bridge_and_delete(self, node_uuid: str) -> dict[str, int]:
        return self._consolidation.bridge_and_delete(node_uuid)

    def fetch_memory_overview(self, namespace: str | None = None) -> dict[str, Any]:
        """Return high-level graph counts, optionally scoped to one silo (CF-33)."""
        return self._memory_queries.fetch_memory_overview(namespace)

    def fetch_recent_memories(
        self, limit: int = 10, namespace: str | None = None
    ) -> list[dict[str, Any]]:
        """Return the most recently accessed or created memory nodes."""
        return self._memory_queries.fetch_recent_memories(limit, namespace=namespace)

    def fetch_flagged_memories(
        self,
        limit: int = 10,
        workspace: str | None = None,
        *,
        namespace: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return explicitly flagged memory nodes for bootstrap context reads."""
        return self._memory_queries.fetch_flagged_memories(
            limit, workspace=workspace, namespace=namespace
        )

    def fetch_flagged_memory_bootstrap_version(
        self,
        workspace: str | None = None,
        *,
        namespace: str | None = None,
    ) -> str:
        """Return a deterministic version fingerprint for the flagged-memory set."""
        return self._memory_queries.fetch_flagged_memory_bootstrap_version(
            workspace=workspace, namespace=namespace
        )

    def unflag_structural_nodes(self) -> int:
        """One-time cleanup: remove user_flagged from structural graph nodes."""
        return self._memory_queries.unflag_structural_nodes()

    def fetch_memory_by_uuid(
        self, node_uuid: str, *, namespace: str | None = None
    ) -> dict[str, Any] | None:
        """Return a single memory node by UUID, optionally restricted to one namespace."""
        return self._memory_queries.fetch_memory_by_uuid(
            node_uuid, namespace=namespace
        )

    def fetch_memories_by_scope(
        self, scope: str, limit: int = 10, *, namespace: str | None = None
    ) -> list[dict[str, Any]]:
        """Return memory nodes filtered by scope, optionally restricted to one namespace."""
        return self._memory_queries.fetch_memories_by_scope(
            scope, limit, namespace=namespace
        )

    def fetch_memories_by_type(
        self, memory_type: str, limit: int = 10, *, namespace: str | None = None
    ) -> list[dict[str, Any]]:
        """Return entity memories filtered by type, optionally restricted to one namespace."""
        return self._memory_queries.fetch_memories_by_type(
            memory_type, limit, namespace=namespace
        )

    def fetch_session_entities(
        self,
        session_id: str | None = None,
        max_age_hours: float = 0,
    ) -> list[dict[str, Any]]:
        return self._consolidation.fetch_session_entities(session_id, max_age_hours)

    def count_persistent_edges(self, node_uuid: str) -> int:
        return self._consolidation.count_persistent_edges(node_uuid)

    def promote_to_persistent(self, node_uuids: list[str]) -> int:
        return self._consolidation.promote_to_persistent(node_uuids)

    def delete_session_nodes(self, node_uuids: list[str]) -> int:
        return self._consolidation.delete_session_nodes(node_uuids)

    def set_demote_ttl(self, node_uuids: list[str], ttl_days: int) -> int:
        return self._consolidation.set_demote_ttl(node_uuids, ttl_days)

    def fetch_ttl_expired_session_uuids(self, session_id: str | None = None) -> list[dict[str, Any]]:
        return self._consolidation.fetch_ttl_expired_session_uuids(session_id)

    def update_sharpness(self, node_uuid: str, sharpness: float) -> bool:
        return self._consolidation.update_sharpness(node_uuid, sharpness)

    def set_conflict(
        self,
        node_uuid_a: str,
        node_uuid_b: str,
        new_group_id: str,
        *,
        initial_status: str = "pending_llm_review",
    ) -> tuple[str, int]:
        return self._consolidation.set_conflict(
            node_uuid_a, node_uuid_b, new_group_id, initial_status=initial_status
        )

    def set_conflict_group_status(self, group_id: str, status: str) -> int:
        return self._consolidation.set_conflict_group_status(group_id, status)

    def requeue_conflicts_for_llm_review(
        self, *, from_status: str = "unresolved", limit: int = 200,
        namespace: str | None = None,
    ) -> int:
        return self._consolidation.requeue_conflicts_for_llm_review(
            from_status=from_status, limit=limit, namespace=namespace
        )

    # -------------------------------------------------------------------------
    # Conflict governance delegates → ConsolidationRepository
    # -------------------------------------------------------------------------

    def list_conflict_groups(
        self,
        *,
        status: str | None = "unresolved",
        limit: int = 25,
        namespace: str | None = None,
        created_before: datetime | None = None,
        oldest_first: bool = False,
    ) -> list[dict[str, Any]]:
        return self._consolidation.list_conflict_groups(
            status=status,
            limit=limit,
            namespace=namespace,
            created_before=created_before,
            oldest_first=oldest_first,
        )

    def list_conflict_pairs(
        self,
        *,
        status: str | None = "unresolved",
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        return self._consolidation.list_conflict_pairs(status=status, limit=limit)

    def bridge_edges_for_node(
        self,
        node_uuid: str,
        *,
        namespace: str | None = None,
    ) -> int:
        return self._consolidation.bridge_edges_for_node(
            node_uuid,
            namespace=namespace,
        )

    def bridge_edges_for_nodes(
        self,
        node_uuids: list[str],
        *,
        namespace: str | None = None,
    ) -> int:
        return self._consolidation.bridge_edges_for_nodes(
            node_uuids,
            namespace=namespace,
        )

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
        return self._consolidation.resolve_conflict_group(
            conflict_group_id,
            action,
            keep_uuid=keep_uuid,
            remove_uuid=remove_uuid,
            resolution_status=resolution_status,
            allow_promoted_removal=allow_promoted_removal,
            namespace=namespace,
        )
