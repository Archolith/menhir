"""CANDIDATE delegates for the memory graph adapter.

Methods moved verbatim from ``memory_graph_adapter.py`` (facade split); this
mixin is composed into :class:`~menhir.infrastructure.memory_graph_adapter.MemoryGraphAdapter`.
"""

from __future__ import annotations

from typing import Any


class MemoryGraphCandidatesMixin:
    """CANDIDATE delegates (mixin for MemoryGraphAdapter)."""

    # -------------------------------------------------------------------------
    # CANDIDATE delegates -> CandidateRepository
    # -------------------------------------------------------------------------

    def create_candidate(
        self,
        *,
        content: str,
        source: str,
        cluster_id: str,
        label: str,
        kind: str = "memory",
        candidate_type: str = "other",
        type: str = "SEMANTIC",
        evidence_strength: str = "REPEATED",
        distinct_sessions: int = 0,
        first_seen: str | None = None,
        last_seen: str | None = None,
        notes: list[str] | None = None,
        source_confidence: float = 0.5,
        namespace: str | None = None,
    ) -> dict[str, Any]:
        return self._candidates.create_candidate(
            content=content,
            source=source,
            cluster_id=cluster_id,
            label=label,
            kind=kind,
            candidate_type=candidate_type,
            type=type,
            evidence_strength=evidence_strength,
            distinct_sessions=distinct_sessions,
            first_seen=first_seen,
            last_seen=last_seen,
            notes=notes,
            source_confidence=source_confidence,
            namespace=namespace,
        )

    def list_candidates(self, *, source: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        return self._candidates.list_candidates(source=source, limit=limit)

    def fetch_candidate(self, uuid: str) -> dict[str, Any] | None:
        return self._candidates.fetch_candidate(uuid)

    def promote_candidate(self, uuid: str) -> bool:
        return self._candidates.promote_candidate(uuid)

    def reject_candidate(self, uuid: str) -> bool:
        return self._candidates.reject_candidate(uuid)
