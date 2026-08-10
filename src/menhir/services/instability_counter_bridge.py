"""MemoryRevision -> Metric bridge — turn belief churn into an operator instability signal.

Sibling of failure_counter_bridge, different behavioral lever:
  FailureEvent counter    -> "this failed 3 times"                -> stop repeating failed actions
  MemoryRevision counter  -> "this belief was corrected 4 times"  -> lower confidence / ask first

Same machinery: ingest records every scope/field change in telemetry.memory_revisions (node_uuid,
field, old/new_value, changed_by, episode_uuid). Fold it deterministically (SQL GROUP BY
node_uuid, field -- no LLM) into supersedable :Metric counters through the MetricWriteCoordinator
saga. These are instrumentation (:Metric, excluded from recall by label, no embedding), not
memories.

A field is 'unstable' only once revised (min_count=2): the count IS the instability signal -- a
value corrected many times should not be asserted confidently. The fold is cutoff-bound and
recorded as the recomputed absolute count (menhir retains telemetry). See the workspace-root Metric
plan (Parts A6/B, scope revision).
"""

from __future__ import annotations

from typing import Any, Protocol

# Fields that record a node's lifecycle/namespace movement, NOT a correction of what it asserts.
# `scope` is the SESSION->PERSISTENT promotion stamp: mechanical churn (one write per promotion,
# ~1 per node), so folding it produces tens of thousands of meaningless "*_revised" counters that
# swamp the real belief-instability signal. Excluded from the fold.
LIFECYCLE_FIELDS: frozenset[str] = frozenset({"scope"})


class _Store(Protocol):
    def fold_revisions_bounded(
        self,
        *,
        cutoff_id: int | None = ...,
        after_id: int = ...,
        min_count: int = ...,
        exclude_fields: Any = ...,
    ) -> dict[str, Any]: ...


class _Coordinator(Protocol):
    def record_telemetry_absolute(self, **kwargs: Any) -> dict[str, Any]: ...


def sync_instability_counters(
    *,
    store: _Store,
    coordinator: _Coordinator,
    namespace: str = "agent-experience",
    min_count: int = 2,
    exclude_fields: Any = LIFECYCLE_FIELDS,
) -> list[dict[str, Any]]:
    """Fold telemetry.memory_revisions into :Metric counters. One counter per (node_uuid, field),
    subject = the node, counter = '<field>_revised'. Idempotent + supersedable via the coordinator
    saga: re-running recomputes the current revision count (a no-op commit when unchanged).

    Cutoff-bound (MAX(id) captured with the fold) and recorded as the absolute count. Returns the
    written counter rows.
    """
    folded = store.fold_revisions_bounded(min_count=min_count, exclude_fields=exclude_fields)
    cutoff = int(folded["cutoff_id"])
    results: list[dict[str, Any]] = []
    for group in folded["groups"]:
        node = str(group["node_uuid"])
        field = str(group["field"]).strip().lower().replace(" ", "_")
        subject = f"belief:{node}"
        counter = f"{field}_revised"
        res = coordinator.record_telemetry_absolute(
            subject=subject,
            counter=counter,
            source_table="memory_revisions",
            grouping={"node_uuid": node, "field": group["field"]},
            cutoff_id=cutoff,
            absolute_count=float(group["count"]),
            row_ids=list(group.get("row_ids") or []),
            namespace=namespace,
            source="revision-telemetry",
        )
        res.update({"node_uuid": node, "field": field, "count": group["count"],
                    "latest_value": group.get("latest_value")})
        results.append(res)
    return results
