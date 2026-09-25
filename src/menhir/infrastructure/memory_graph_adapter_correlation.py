"""CORRELATION (merge/related-to) delegates for the memory graph adapter.

Methods moved verbatim from ``memory_graph_adapter.py`` (facade split); this
mixin is composed into :class:`~menhir.infrastructure.memory_graph_adapter.MemoryGraphAdapter`.
"""

from __future__ import annotations

from typing import Any


class MemoryGraphCorrelationMixin:
    """CORRELATION (merge/related-to) delegates (mixin for MemoryGraphAdapter)."""

    # -------------------------------------------------------------------------
    # CORRELATION delegates → CorrelationRepository
    # -------------------------------------------------------------------------

    def create_related_to_edge(
        self,
        source_uuid: str,
        target_uuid: str,
        *,
        similarity: float,
        source: str = "correlation-detected",
    ) -> bool:
        return self._correlation.create_related_to_edge(
            source_uuid, target_uuid, similarity=similarity, source=source,
        )

    def merge_entity(
        self,
        survivor_uuid: str,
        absorbed_uuid: str,
        *,
        similarity: float,
        operation_id: str | None = None,
    ) -> dict[str, int]:
        return self._correlation.merge_entity(
            survivor_uuid, absorbed_uuid, similarity=similarity, operation_id=operation_id,
        )

    def correlation_exists(self, uuid_a: str, uuid_b: str) -> bool:
        return self._correlation.correlation_exists(uuid_a, uuid_b)

    def fetch_entity_merge_metadata(self, uuids: list[str]) -> list[dict[str, Any]]:
        return self._correlation.fetch_entity_merge_metadata(uuids)

    def check_ineligible_node_veto(self, survivor_uuid: str, absorbed_uuid: str) -> bool:
        return self._correlation.check_ineligible_node_veto(survivor_uuid, absorbed_uuid)

    def evaluate_merge_eligibility(self, survivor_uuid: str, absorbed_uuid: str) -> Any:
        return self._correlation.evaluate_merge_eligibility(survivor_uuid, absorbed_uuid)

    def capture_merge_snapshot(
        self, survivor_uuid: str, absorbed_uuid: str, *, similarity: float | None = None
    ) -> dict[str, Any]:
        return self._correlation.capture_merge_snapshot(
            survivor_uuid, absorbed_uuid, similarity=similarity
        )

    def fetch_merge_state(self, survivor_uuid: str, absorbed_uuid: str) -> dict[str, Any]:
        return self._correlation.fetch_merge_state(survivor_uuid, absorbed_uuid)

    def fetch_survivor_properties(self, survivor_uuid: str) -> dict[str, Any] | None:
        return self._correlation.fetch_survivor_properties(survivor_uuid)

    def peers_exist(self, uuids: list[str]) -> set[str]:
        return self._correlation.peers_exist(uuids)

    def restore_merge_snapshot(self, **kwargs: Any) -> dict[str, Any]:
        return self._correlation.restore_merge_snapshot(**kwargs)

    def capture_node_state(self, uuid: str) -> dict[str, Any] | None:
        return self._correlation.capture_node_state(uuid)

    def delete_entities_returning_uuids(
        self,
        node_uuids: list[str],
        *,
        require_scope: str | None = None,
        protect_retention: bool = False,
    ) -> list[str]:
        return self._consolidation.delete_entities_returning_uuids(
            node_uuids,
            require_scope=require_scope,
            protect_retention=protect_retention,
        )

    def newly_unreferenced_evidence(self, node_uuids: list[str]) -> list[str]:
        return self._consolidation.newly_unreferenced_evidence(node_uuids)

    def check_co_mention_veto(self, uuid_a: str, uuid_b: str) -> bool:
        return self._correlation.check_co_mention_veto(uuid_a, uuid_b)

    def check_anchor_project_veto(self, uuid_a: str, uuid_b: str) -> bool:
        return self._correlation.check_anchor_project_veto(uuid_a, uuid_b)
