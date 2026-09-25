"""VIEW delegates (counter/timeline/scalar-state/metric views) for the memory graph adapter.

Methods moved verbatim from ``memory_graph_adapter.py`` (facade split); this
mixin is composed into :class:`~menhir.infrastructure.memory_graph_adapter.MemoryGraphAdapter`.
"""

from __future__ import annotations

from typing import Any


class MemoryGraphViewsMixin:
    """VIEW delegates (mixin for MemoryGraphAdapter)."""

    # -------------------------------------------------------------------------
    # VIEW delegates → ViewRepository (one supersedable View shape, many kinds)
    #   kind='counter' (QuantState): record/fetch/list/history_counter
    #   kind='timeline'            : record/fetch_timeline
    # -------------------------------------------------------------------------

    def record_counter(self, **kwargs: Any) -> dict[str, Any]:
        return self._views.record_counter(**kwargs)

    def fetch_counter(self, *, subject: str, counter: str, namespace: str | None = None
                      ) -> dict[str, Any] | None:
        return self._views.fetch_counter(subject=subject, counter=counter, namespace=namespace)

    def list_counters(self, *, namespace: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        return self._views.list_counters(namespace=namespace, limit=limit)

    def counter_history(self, *, subject: str, counter: str, namespace: str | None = None
                        ) -> list[dict[str, Any]]:
        return self._views.history(subject=subject, counter=counter, namespace=namespace)

    def record_scalar_state(self, **kwargs: Any) -> dict[str, Any]:
        return self._views.record_scalar_state(**kwargs)

    def fetch_scalar_state(self, **kwargs: Any) -> dict[str, Any] | None:
        return self._views.fetch_scalar_state(**kwargs)

    def fetch_current_scalar_view_for_slot(self, **kwargs: Any) -> dict[str, Any] | None:
        """Slot-keyed current scalar_state View for deterministic recall injection (Phase 4a.4)."""
        return self._views.fetch_current_scalar_view_for_slot(**kwargs)

    def fetch_scalar_authority_contributors(self, **kwargs: Any) -> dict[str, Any]:
        """Bounded labeled provenance for the structured authority layer (Phase 4c/7.J)."""
        return self._views.fetch_scalar_authority_contributors(**kwargs)

    def scalar_view_has_user_foundation(self, **kwargs: Any) -> bool:
        """True if the View's head traces to a declarant='user' :TurnEvidence foundation (G14/10.G)."""
        return self._views.scalar_view_has_user_foundation(**kwargs)

    def draw_scalar_state_provenance_edges(self, **kwargs: Any) -> dict[str, int]:
        """Draw a scalar_state View's CURRENT_ANCHOR/CONTRIBUTED_TO/SUPERSEDED_ANCHOR edges (Phase 3).

        REQUIRED: `scalar_state_service()` injects THIS adapter as the View sink, and
        ScalarStateService.rebuild_scalar_state only draws edges when
        `hasattr(self._views, "draw_scalar_state_provenance_edges")`. Without this passthrough the guard
        is False and the edges are SILENTLY skipped in the production consolidation path (they were only
        ever drawn by tests that injected a ViewRepository directly), so a View can never trace to its
        anchor and the G14/10.G foundation gate can never let it lead."""
        return self._views.draw_scalar_state_provenance_edges(**kwargs)

    def assertions_have_user_foundation(self, **kwargs: Any) -> bool:
        """True if any given assertion has a declarant='user' :TurnEvidence FOUNDS (G13 expiry basis)."""
        return self._views.assertions_have_user_foundation(**kwargs)

    def list_scalar_state_views(self, **kwargs: Any) -> list[dict[str, Any]]:
        return self._views.list_scalar_state_views(**kwargs)

    def retire_scalar_state(self, **kwargs: Any) -> bool:
        return self._views.retire_scalar_state(**kwargs)

    def retire_counters_superseded_by_scalar(self, *, namespace: str) -> int:
        return self._views.retire_counters_superseded_by_scalar(namespace=namespace)

    # --- scalar_history (advisory ordered assertion history per slot) ---

    def record_scalar_history(self, **kwargs: Any) -> dict[str, Any]:
        return self._views.record_scalar_history(**kwargs)

    def fetch_scalar_history(self, **kwargs: Any) -> dict[str, Any] | None:
        return self._views.fetch_scalar_history(**kwargs)

    def list_scalar_history_views(self, **kwargs: Any) -> list[dict[str, Any]]:
        return self._views.list_scalar_history_views(**kwargs)

    def list_scalar_history_views_for_namespace(
        self, **kwargs: Any
    ) -> list[dict[str, Any]]:
        return self._views.list_scalar_history_views_for_namespace(**kwargs)

    def retire_scalar_history(self, **kwargs: Any) -> bool:
        return self._views.retire_scalar_history(**kwargs)

    def draw_scalar_history_entries(self, **kwargs: Any) -> dict[str, int]:
        return self._views.draw_scalar_history_entries(**kwargs)

    def list_scalar_history_entries(self, **kwargs: Any) -> dict[str, Any]:
        return self._views.list_scalar_history_entries(**kwargs)

    def record_timeline(self, **kwargs: Any) -> dict[str, Any]:
        return self._views.record_timeline(**kwargs)

    def fetch_timeline(self, *, subject: str, namespace: str | None = None
                       ) -> dict[str, Any] | None:
        return self._views.fetch_timeline(subject=subject, namespace=namespace)

    def list_views(self, *, kind: str | None = None, namespace: str | None = None,
                   limit: int = 100) -> list[dict[str, Any]]:
        return self._views.list_views(kind=kind, namespace=namespace, limit=limit)

    # --- event-lane timeline (Event History Phase 2B.2) ---

    def record_event_timeline(self, **kwargs: Any) -> dict[str, Any]:
        """Upsert the entity-anchored, predicate/domain event-lane timeline View."""
        return self._views.record_event_timeline(**kwargs)

    def fetch_event_timeline(self, **kwargs: Any) -> dict[str, Any] | None:
        """Current event-lane timeline View for (subject_uuid, predicate[, domain])."""
        return self._views.fetch_event_timeline(**kwargs)

    def list_event_timeline_views(self, **kwargs: Any) -> list[dict[str, Any]]:
        """Current event-lane timeline Views for one resolved entity."""
        return self._views.list_event_timeline_views(**kwargs)

    def retire_event_timeline(self, **kwargs: Any) -> bool:
        """Expire a current event-lane timeline View with no replacement."""
        return self._views.retire_event_timeline(**kwargs)

    def draw_event_timeline_entries(self, **kwargs: Any) -> dict[str, int]:
        """Atomically rewrite a View's EVENT_HISTORY_ENTRY edges to an exact ordered assertion set."""
        return self._views.draw_event_timeline_entries(**kwargs)

    def list_event_timeline_entries(self, **kwargs: Any) -> dict[str, Any]:
        """Paginated EVENT_HISTORY_ENTRY contributors for one event-lane timeline View."""
        return self._views.list_event_timeline_entries(**kwargs)

    # --- Metric class (operator-only instrumentation; :Metric label, Metric plan A3) ---

    def record_metric(self, **kwargs: Any) -> dict[str, Any]:
        return self._views.record_metric(**kwargs)

    def fetch_metric(self, *, subject: str, counter: str, namespace: str | None = None
                     ) -> dict[str, Any] | None:
        return self._views.fetch_metric(subject=subject, counter=counter, namespace=namespace)

    def fetch_metric_state(self, *, view_key: str) -> dict[str, Any] | None:
        return self._views.fetch_metric_state(view_key=view_key)

    def metric_history(self, *, subject: str, counter: str, namespace: str | None = None
                       ) -> list[dict[str, Any]]:
        return self._views.metric_history(subject=subject, counter=counter, namespace=namespace)

    def list_metrics(self, *, namespace: str | None = None, source: str | None = None,
                     current_only: bool = True, limit: int = 100) -> list[dict[str, Any]]:
        return self._views.list_metrics(
            namespace=namespace, source=source, current_only=current_only, limit=limit
        )

    def record_admission_audit(
        self, *, subject: str, namespace: str | None = None,
        requested_source: str = "", effective_source: str = "",
        granted: bool = False, turn_evidence_uuid: str | None = None,
        reason: str = "", source: str = "admission_gate", source_confidence: float = 0.6,
    ) -> dict[str, Any]:
        """Record an admission verdict audit row via View storage."""
        return self._views.record(
            "admission_audit",
            subject=subject,
            namespace=namespace,
            source=source,
            source_confidence=source_confidence,
            requested_source=requested_source,
            effective_source=effective_source,
            granted=granted,
            turn_evidence_uuid=turn_evidence_uuid,
            reason=reason,
        )
