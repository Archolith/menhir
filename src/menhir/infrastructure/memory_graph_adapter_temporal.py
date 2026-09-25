"""TEMPORAL delegates for the memory graph adapter.

Methods moved verbatim from ``memory_graph_adapter.py`` (facade split); this
mixin is composed into :class:`~menhir.infrastructure.memory_graph_adapter.MemoryGraphAdapter`.
"""

from __future__ import annotations

from typing import Any


class MemoryGraphTemporalMixin:
    """TEMPORAL delegates (mixin for MemoryGraphAdapter)."""

    # -------------------------------------------------------------------------
    # TEMPORAL delegates → TemporalRepository
    # -------------------------------------------------------------------------

    def create_temporal(
        self,
        *,
        content: str,
        target_date: str,
        source: str = "claude-code",
        name: str | None = None,
        flagged: bool = False,
        bootstrap_scope: str | None = None,
        namespace: str | None = None,
        turn_evidence_uuid: str | None = None,
    ) -> dict[str, Any]:
        return self._temporal.create_temporal(
            content=content,
            target_date=target_date,
            source=source,
            name=name,
            user_flagged=flagged,
            bootstrap_scope=bootstrap_scope,
            namespace=namespace,
            turn_evidence_uuid=turn_evidence_uuid,
            turn_evidence_repo=self._turn_evidence,
            audit_recorder=self.record_admission_audit,
        )

    def list_temporal_in_window(
        self, *, window_days: int = 30, namespace: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        return self._temporal.list_in_window(
            window_days=window_days, namespace=namespace, limit=limit
        )

    def complete_temporal(self, uuid: str) -> bool:
        return self._temporal.complete_temporal(uuid)
