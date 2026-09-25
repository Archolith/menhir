"""L4 ARTIFACT delegates for the memory graph adapter.

Methods moved verbatim from ``memory_graph_adapter.py`` (facade split); this
mixin is composed into :class:`~menhir.infrastructure.memory_graph_adapter.MemoryGraphAdapter`.
"""

from __future__ import annotations

from typing import Any


class MemoryGraphL4ArtifactsMixin:
    """L4 ARTIFACT delegates (mixin for MemoryGraphAdapter)."""

    # -------------------------------------------------------------------------
    # L4 ARTIFACT delegates → ArtifactRepository
    # -------------------------------------------------------------------------

    def create_artifact(
        self,
        *,
        artifact_id: str,
        artifact_type: str,
        summary: str,
        source: str,
        status: str,
        body: str = "",
        evidence: list[dict[str, Any]] | None = None,
        anchors: list[str] | None = None,
        source_confidence: float = 0.5,
    ) -> dict[str, Any]:
        return self._artifacts.create_artifact(
            artifact_id=artifact_id,
            artifact_type=artifact_type,
            summary=summary,
            source=source,
            status=status,
            body=body,
            evidence=evidence,
            anchors=anchors,
            source_confidence=source_confidence,
        )

    def promote_artifact(self, artifact_id: str, *, trusted_confidence: float = 0.9) -> bool:
        return self._artifacts.promote_artifact(artifact_id, trusted_confidence=trusted_confidence)

    def supersede_l4_artifact(self, old_id: str, new_id: str) -> bool:
        return self._artifacts.supersede_artifact(old_id, new_id)

    def find_artifacts(
        self, *, tokens: list[str] | None = None, anchors: list[str] | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        return self._artifacts.find_artifacts(tokens=tokens, anchors=anchors, limit=limit)

    def fetch_artifact(self, artifact_id: str) -> dict[str, Any] | None:
        return self._artifacts.fetch_artifact(artifact_id)
