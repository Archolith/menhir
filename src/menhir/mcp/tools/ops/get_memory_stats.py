"""MCP tool: get_memory_stats."""

from __future__ import annotations

from menhir.mcp.tools.base import BaseTextTool
from menhir.mcp.contracts import ToolScope


async def get_memory_stats(since_hours: int = 24) -> str:
    """Return operational health summary: ingestion rate, recall latency, failure counts, enrichment queue depth.

    Args:
        since_hours: Lookback window in hours (default: 24).
    """

    return await GetMemoryStatsTool().execute(since_hours=since_hours)


# Operations that are user-facing (not scheduler background noise)
_USER_OPS = frozenset({
    "add_memory", "recall_memories", "recall_context_memories", "build_context",
    "read_flagged_memories", "flag_memory", "delete_memory", "resolve_conflict",
    "list_conflicts", "get_enrichment_status", "force_release_enrichment_lease",
    "ingest_project", "query_structure", "get_memory_stats",
})

_SCHEDULER_PREFIX = "scheduler_"


class GetMemoryStatsTool(BaseTextTool):
    name = "get_memory_stats"
    # GLOBAL, not OBJECT: this is the operational health dashboard -- operation latencies,
    # failure counts, enrichment rate, queue depth, circuit-breaker state. None of it is tenant
    # memory and none of it is addressed by an object, so OBJECT was a misdeclaration rather
    # than a missing namespace argument.
    #
    # Residue, stated rather than glossed: the Graph section reports AGGREGATE counts
    # (entity_count, episode_count, flagged_count) summed across every silo, so a pinned client
    # learns roughly how much data exists outside its own. That is a cardinality signal, not
    # content, and it is the same shape as the other GLOBAL tools' operational readouts.
    # Narrowing it means per-namespace counts, which is a feature decision, not this fix.
    scope = ToolScope.GLOBAL
    required_tier = "readonly"
    description = "Return operational health summary."

    async def endpoint(self, since_hours: int = 24) -> str:
        backend = self.get_backend()

        overview = await backend.fetch_memory_overview()
        op_stats = await backend.fetch_operation_stats(since_hours=since_hours)
        failure_summary = await backend.fetch_failure_summary(since_hours=since_hours)
        enrichment = await backend.fetch_enrichment_rate(since_hours=since_hours)
        lifecycle = await backend.fetch_lifecycle_summary(since_hours=since_hours)
        queue_depth = await backend.get_queue_depth()
        breakers = await backend.circuit_breaker_snapshots()

        ops_by_name: dict[str, dict] = {s["operation"]: s for s in op_stats}

        lines: list[str] = []
        lines.append(f"## menhir — last {since_hours}h")
        lines.append("")

        # ── Graph Overview ──
        lines.append("### Graph")
        lines.append(f"  Entities:    {overview.get('entity_count', '?'):>6}")
        lines.append(f"  Episodes:    {overview.get('episode_count', '?'):>6}")
        lines.append(f"  Flagged:     {overview.get('flagged_count', '?'):>6}")
        lines.append(f"  Persistent:  {overview.get('persistent_count', '?'):>6}")
        lines.append(f"  Session:     {overview.get('session_count', '?'):>6}")
        lines.append("")

        # ── Enrichment Pipeline ──
        lines.append("### Enrichment")
        total = enrichment["total"]
        ok = enrichment["successes"]
        failed_enrichment = total - ok
        avg_s = f"{enrichment['avg_duration_ms'] / 1000:.1f}s" if enrichment["avg_duration_ms"] else "n/a"
        p95_ms = enrichment.get("p95_duration_ms")
        slo_target_ms = 120_000
        if p95_ms is not None:
            p95_s = f"{p95_ms / 1000:.1f}s"
            slo_flag = " ok" if p95_ms <= slo_target_ms else " MISS"
            p95_str = f"{p95_s}  (p95 SLO:{slo_flag})"
        else:
            p95_str = "n/a"
        lines.append(f"  Queued:      {total:>6}")
        lines.append(f"  Completed:   {ok:>6}  ({enrichment['success_rate_pct']}%)")
        if failed_enrichment > 0:
            lines.append(f"  Failed:      {failed_enrichment:>6}  (in window)")
        # Standing FAILED count comes from the GRAPH, not the telemetry window: a backlog
        # older than since_hours is invisible in the line above. These episodes keep their
        # content but have no entities, and recall searches entities -- so they are
        # unrecallable despite add_memory having reported success. Most are never retried
        # automatically (terminal/manual_review); the scheduler's queue-health job logs the
        # classification breakdown.
        standing_failed = int(overview.get("failed_count") or 0)
        if standing_failed > 0:
            lines.append(f"  STUCK:       {standing_failed:>6}  <- unrecallable, mostly not auto-retried")
        lines.append(f"  Pending now: {queue_depth:>6}")
        enriching = overview.get("enriching_count", 0)
        if enriching:
            lines.append(f"  Enriching:   {enriching:>6}")
        lines.append(f"  Avg time:    {avg_s:>6}")
        lines.append(f"  p95 time:    {p95_str}")
        lines.append("")

        # ── User Operations ──
        lines.append("### Operations")
        user_ops = [(name, ops_by_name[name]) for name in sorted(ops_by_name) if name in _USER_OPS]
        if user_ops:
            for name, s in user_ops:
                calls = s["total_calls"]
                p50 = s.get("p50_ms")
                p50_str = f"{p50}ms" if p50 is not None else ""
                rate = s.get("success_rate_pct", 100)
                status = "" if rate == 100 else f"  ({rate}% ok)"
                lines.append(f"  {name}: {calls} calls  p50={p50_str}{status}")
        else:
            lines.append("  No user operations in window")
        lines.append("")

        # ── Failures ──
        # Separate enrichment failures from scheduler background noise
        enrichment_failures = failure_summary.get("by_operation", {})
        user_failures = {op: cnt for op, cnt in enrichment_failures.items() if not op.startswith(_SCHEDULER_PREFIX)}
        scheduler_failures = {op: cnt for op, cnt in enrichment_failures.items() if op.startswith(_SCHEDULER_PREFIX)}
        user_failure_total = sum(user_failures.values())
        scheduler_failure_total = sum(scheduler_failures.values())

        if user_failure_total > 0:
            lines.append("### Failures")
            for op, cnt in sorted(user_failures.items(), key=lambda x: -x[1]):
                lines.append(f"  {op}: {cnt}")
            lines.append("")

        if scheduler_failure_total > 0:
            lines.append(f"### Scheduler Issues ({scheduler_failure_total} total)")
            for op, cnt in sorted(scheduler_failures.items(), key=lambda x: -x[1]):
                short_name = op.removeprefix("scheduler_")
                lines.append(f"  {short_name}: {cnt}")
            lines.append("")

        if user_failure_total == 0 and scheduler_failure_total == 0:
            lines.append("### Failures")
            lines.append("  None")
            lines.append("")

        # ── Lifecycle ──
        compress_count = lifecycle.get("compress", {}).get("count", 0)
        rehydrate_count = lifecycle.get("rehydrate", {}).get("count", 0)
        if compress_count > 0 or rehydrate_count > 0:
            lines.append("### Lifecycle")
            if compress_count:
                lines.append(f"  Compressions:  {compress_count}")
            if rehydrate_count:
                lines.append(f"  Rehydrations:  {rehydrate_count}")
            lines.append("")

        # ── Circuit Breakers (only show if any are open/half-open) ──
        tripped = {k: v for k, v in breakers.items() if v["state"].lower() != "closed"}
        if tripped:
            lines.append("### Circuit Breakers")
            for kind, snap in tripped.items():
                lines.append(f"  {kind}: {snap['state'].upper()} ({snap['failures']} failures)")
            lines.append("")

        return "\n".join(lines)
