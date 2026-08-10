"""FailureEvent -> Metric bridge — turn recorded failures into operator instrumentation.

Ingest records structured failure events (telemetry.failure_events: operation, error_type,
classification, retryable, processing_attempt, episode_uuid, ...). This folds them deterministically
(SQL GROUP BY -- no LLM, the events are already typed) into supersedable :Metric counters, so an
operator can ask "how often has enrichment failed on X?" without that instrumentation competing with
real memories in the semantic-recall layer.

These are :Metric, not :Entity: instrumentation, excluded from recall by label, no embedding. Each
write goes through the MetricWriteCoordinator saga (durable receipt, crash-safe). The fold is
cutoff-bound and recorded as the recomputed ABSOLUTE lifetime count -- menhir retains telemetry, so
the simple always-correct recompute is the honest choice (the chained-accumulator path exists for
anyone who prunes). See the workspace-root Metric plan (Parts A6/B, and the scope revision).
"""

from __future__ import annotations

from typing import Any, Protocol


class _Store(Protocol):
    def fold_failures_bounded(
        self, *, cutoff_id: int | None = ..., after_id: int = ..., min_count: int = ...
    ) -> dict[str, Any]: ...


class _Coordinator(Protocol):
    def record_telemetry_absolute(self, **kwargs: Any) -> dict[str, Any]: ...


def sync_failure_counters(
    *,
    store: _Store,
    coordinator: _Coordinator,
    namespace: str = "agent-experience",
    subject: str = "enrichment",
    min_count: int = 1,
) -> list[dict[str, Any]]:
    """Fold telemetry.failure_events into :Metric counters. One counter per (operation, error_type),
    keyed '<operation>_<error_type>_failed'. Idempotent + supersedable via the coordinator saga:
    re-running recomputes the current lifetime count (a no-op commit when unchanged).

    The fold is cutoff-bound (MAX(id) captured with the fold, so rows arriving mid-sweep belong to
    the next receipt) and recorded as the absolute count. Returns the written counter rows.
    """
    folded = store.fold_failures_bounded(min_count=min_count)
    cutoff = int(folded["cutoff_id"])
    results: list[dict[str, Any]] = []
    for group in folded["groups"]:
        op = str(group["operation"]).strip().lower().replace(" ", "_")
        err = str(group["error_type"]).strip().lower().replace(" ", "_")
        counter = f"{op}_{err}_failed"
        res = coordinator.record_telemetry_absolute(
            subject=subject,
            counter=counter,
            source_table="failure_events",
            grouping={"operation": group["operation"], "error_type": group["error_type"]},
            cutoff_id=cutoff,
            absolute_count=float(group["count"]),
            row_ids=list(group.get("row_ids") or []),
            namespace=namespace,
            source="failure-telemetry",
        )
        res.update({"counter": counter, "count": group["count"]})
        results.append(res)
    return results
