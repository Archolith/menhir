"""Personal-memory consolidation, turn-evidence and tool-event delegates.

Methods moved verbatim from ``memory_graph_adapter.py`` (facade split); this
mixin is composed into :class:`~menhir.infrastructure.memory_graph_adapter.MemoryGraphAdapter`.
"""

from __future__ import annotations

from typing import Any


class MemoryGraphPerceptionMixin:
    """Personal-memory consolidation, turn-evidence and tool-event delegates (mixin for MemoryGraphAdapter)."""

    # --- personal-memory consolidation (perception) ---
    # Prefer raw :Turn evidence (ADR 0001) when any user-authored Turn exists; otherwise fall back to
    # the legacy `user:`-prefixed Episodic path (benchmark fixtures). The switch is per-call so a box
    # that starts capturing Turns transitions with no restart.
    def list_dirty_namespaces(self, *, limit: int = 200) -> list[str]:
        if self._turn_evidence.evidence_exists():
            return self._turn_evidence.list_dirty_evidence_namespaces(limit=limit)
        return self._personal_memory.list_dirty_namespaces(limit=limit)

    def load_user_episodes(self, namespace: str, *, limit: int = 500) -> list[dict[str, Any]]:
        if self._turn_evidence.evidence_exists():
            return self._turn_evidence.load_user_evidence(namespace, limit=limit)
        return self._personal_memory.load_user_episodes(namespace, limit=limit)

    def mark_consolidated(self, namespace: str, *, at: str) -> None:
        self._personal_memory.mark_consolidated(namespace, at=at)

    def list_scalar_dirty_namespaces(
        self, *, perceiver_version: str, limit: int = 200
    ) -> list[str]:
        """Namespaces due for typed-scalar consolidation (ScalarStateView C.4.3) per the independent,
        version-stamped :ScalarConsolidationWatermark cursor — NOT the counter watermark. Dirty when
        never scalar-consolidated, consolidated by a different perceiver_version, or carrying episodes
        beyond the stored cursor.

        G14 bridge: prefers raw :TurnEvidence when any user-authored Turn exists (mirrors the counter
        path's per-call switch above), so the typed-scalar path discovers user input -- and grounds its
        assertions to the declarant foundation -- in a Turn-capturing production box; otherwise falls
        back to the legacy `user:`-prefixed Episodic path (benchmark fixtures)."""
        if self._turn_evidence.evidence_exists():
            return self._turn_evidence.list_scalar_dirty_evidence_namespaces(
                perceiver_version=perceiver_version, limit=limit)
        return self._personal_memory.list_scalar_dirty_namespaces(
            perceiver_version=perceiver_version, limit=limit)

    def load_next_scalar_batch(
        self, namespace: str, *, perceiver_version: str, limit: int = 500
    ) -> list[dict[str, Any]]:
        """The next bounded page of USER episodes AFTER the namespace's scalar cursor (C.4.3
        truncation-safe backfill). See PersonalMemoryRepository.load_next_scalar_batch. G14: reads
        :TurnEvidence (turn_id as the grounding anchor) when Turn evidence exists, else Episodic."""
        if self._turn_evidence.evidence_exists():
            return self._turn_evidence.load_next_scalar_evidence_batch(
                namespace, perceiver_version=perceiver_version, limit=limit)
        return self._personal_memory.load_next_scalar_batch(
            namespace, perceiver_version=perceiver_version, limit=limit)

    def advance_scalar_cursor(
        self, namespace: str, *, cursor_at: str, cursor_uuid: str,
        perceiver_version: str, at: str
    ) -> None:
        """Advance the namespace's scalar cursor to the last processed episode's monotonic
        work-discovery key `cursor_at` (C.4.3, NOT world-time). Called only after a batch actually
        ran, so a partial backfill resumes without stranding the tail."""
        self._personal_memory.advance_scalar_cursor(
            namespace, cursor_at=cursor_at, cursor_uuid=cursor_uuid,
            perceiver_version=perceiver_version, at=at)

    # --- event-consolidation cursor (Event History consolidation source) ---
    # These delegates read canonical :TurnEvidence ONLY — no Episodic fallback and no global
    # `evidence_exists` switch. They advance an independent :EventConsolidationWatermark keyed by the
    # namespace string in `group_id`, so event consolidation never disturbs the scalar/counter cursors.
    def list_event_dirty_evidence_namespaces(
        self, *, perceiver_version: str, limit: int = 200,
    ) -> list[str]:
        return self._turn_evidence.list_event_dirty_evidence_namespaces(
            perceiver_version=perceiver_version, limit=limit)

    def load_next_event_evidence_batch(
        self, namespace: str, *, perceiver_version: str, limit: int = 500,
    ) -> list[dict[str, Any]]:
        return self._turn_evidence.load_next_event_evidence_batch(
            namespace, perceiver_version=perceiver_version, limit=limit)

    def advance_event_cursor(
        self, namespace: str, *, cursor_at: str, cursor_uuid: str,
        perceiver_version: str, at: str,
    ) -> None:
        self._turn_evidence.advance_event_cursor(
            namespace, cursor_at=cursor_at, cursor_uuid=cursor_uuid,
            perceiver_version=perceiver_version, at=at)

    # --- selective :TurnEvidence capture (ADR 0001) ---
    def record_turn_evidence(self, **kwargs: Any) -> dict[str, Any]:
        return self._turn_evidence.record_turn_evidence(**kwargs)

    def fetch_turn_evidence(self, turn_id: str) -> dict[str, Any] | None:
        """Fetch one :TurnEvidence node by its turn_id for admission gating."""
        return self._turn_evidence.fetch_by_uuid(turn_id)

    def load_preceding_turn_evidence_context(
        self,
        turn_id: str,
        *,
        namespace: str,
        limit: int = 2,
    ) -> list[dict[str, Any]]:
        """Load adjacent dialogue turns for a bounded relationless-extraction repair.

        ``namespace`` is the CALLER's namespace and is required: `turn_id` is caller-supplied,
        so without it a foreign turn's text reaches this namespace's extraction (CF-236).
        """
        return self._turn_evidence.load_preceding_context(
            turn_id, namespace=namespace, limit=limit
        )

    def turn_evidence_stats(self) -> dict[str, Any]:
        return self._turn_evidence.evidence_stats()

    def count_turn_evidence(self, namespace: str) -> int:
        return self._turn_evidence.count_namespace(namespace)

    def purge_turn_evidence(self, namespace: str) -> int:
        """Delete `:TurnEvidence` for a namespace (not covered by group_id-keyed delete_namespace)."""
        return self._turn_evidence.purge_namespace(namespace)

    # --- Hook Center tool/file events (v0): deterministic dirty/stale marking ---
    def record_file_event(self, **kwargs: Any) -> dict[str, Any]:
        return self._tool_events.record_file_event(**kwargs)

    def list_dirty_files(self, **kwargs: Any) -> list[dict[str, Any]]:
        return self._tool_events.list_dirty_files(**kwargs)

    def stale_anchored_memories(self, **kwargs: Any) -> list[dict[str, Any]]:
        return self._tool_events.stale_anchored_memories(**kwargs)

    def clear_file_dirty(self, **kwargs: Any) -> int:
        return self._tool_events.clear_file_dirty(**kwargs)

    def tool_event_dirty_stats(self, **kwargs: Any) -> dict[str, Any]:
        return self._tool_events.dirty_stats(**kwargs)

    def record_stale_anchor_verification(self, **kwargs: Any) -> dict[str, Any]:
        return self._tool_events.record_stale_anchor_verification(**kwargs)

    def list_stale_anchor_verifications(self, **kwargs: Any) -> list[dict[str, Any]]:
        return self._tool_events.list_stale_anchor_verifications(**kwargs)

    def latest_stale_anchor_verifications(self, **kwargs: Any) -> dict[str, dict[str, Any]]:
        return self._tool_events.latest_stale_anchor_verifications(**kwargs)
