"""Scheduler instrumentation-sync task functions.

Extracted verbatim from ``scheduler_tasks.py``; the facade module re-exports everything
here so existing ``menhir.services.scheduler_tasks`` import sites keep working unchanged.
"""

from __future__ import annotations

import asyncio
from typing import Callable

from menhir.services.failure_counter_bridge import sync_failure_counters
from menhir.services.instability_counter_bridge import sync_instability_counters
from menhir.services.scheduler_protocols import SchedulerGraphAdapter


# ---------------------------------------------------------------------------
# Job: sync experience counters
# ---------------------------------------------------------------------------

async def sync_experience_counters(
    graph_adapter: SchedulerGraphAdapter,
    *,
    namespace: str = "agent-experience",
    embed: Callable[[str], "list[float] | None"] | None = None,
) -> dict[str, object]:
    """Fold telemetry events into supersedable :Metric instrumentation counters.

    Two deterministic folds over the telemetry store (no LLM): failure_events ->
    '<op>_<err>_failed' counters, and memory_revisions -> '<field>_revised' belief-instability
    counters. Both write via the MetricWriteCoordinator saga as :Metric nodes -- instrumentation,
    excluded from semantic recall by label. Runs on the maintenance loop in prod; disabled in
    benchmark mode with the rest of the scheduler.

    `embed` is accepted for caller compatibility but IGNORED: Metrics do not carry a
    name_embedding (they never rank in recall), so there is nothing to embed. It can be dropped
    once the scheduler stops constructing an experience embedder.
    """
    del embed  # vestigial: Metrics do not embed
    from menhir.infrastructure.telemetry import telemetry_store
    from menhir.infrastructure.graph_operations import GraphOperationsJournal
    from menhir.infrastructure.metric_receipts import MetricReceiptStore
    from menhir.services.metric_write_coordinator import MetricWriteCoordinator

    # The coordinator, journal, and receipts all resolve to the telemetry sidecar DB (the journal
    # and receipts default to default_telemetry_db_path, and the coordinator's db defaults to the
    # journal's), so the saga's PREPARED journal-row + receipt commit atomically in one connection.
    coordinator = MetricWriteCoordinator(
        graph_adapter=graph_adapter,
        journal=GraphOperationsJournal(),
        receipts=MetricReceiptStore(),
        namespace=namespace,
    )

    # sync_failure_counters / sync_instability_counters are synchronous and perform blocking I/O
    # (SQLite fold + Neo4j saga). Run them on a worker thread so the maintenance loop never blocks
    # the asyncio event loop. Mirrors refresh_structure_graphs.
    failures = await asyncio.to_thread(
        sync_failure_counters,
        store=telemetry_store, coordinator=coordinator, namespace=namespace,
    )
    instability = await asyncio.to_thread(
        sync_instability_counters,
        store=telemetry_store, coordinator=coordinator, namespace=namespace,
    )
    return {
        "failure_counters": len(failures),
        "instability_counters": len(instability),
    }


# ---------------------------------------------------------------------------
# Job: sync graph-native verifiers
# ---------------------------------------------------------------------------

async def sync_verifiers_job(
    graph_adapter: SchedulerGraphAdapter,
    *,
    verifier_repo: object,
    verifier_context: object,
    namespace: str = "agent-status",
    embed: Callable[[str], "list[float] | None"] | None = None,
) -> dict[str, object]:
    """Re-derive config/status registers from their source of truth via graph verifiers and flag
    referencing beliefs on change. Blocking (Neo4j reads/writes + embed HTTP) so it runs on a worker
    thread, mirroring sync_experience_counters — the maintenance loop must never block the event loop."""
    from menhir.services.verifier_sync import sync_verifiers

    results = await asyncio.to_thread(
        sync_verifiers,
        repo=verifier_repo, graph_adapter=graph_adapter, context=verifier_context,
        namespace=namespace, embed=embed,
    )
    refreshed = sum(1 for r in results if r.get("status") == "refreshed")
    changed = sum(1 for r in results if r.get("changed"))
    flagged = sum(int(r.get("beliefs_flagged") or 0) for r in results)
    return {
        "verifiers": len(results),
        "refreshed": refreshed,
        "changed": changed,
        "beliefs_flagged": flagged,
    }
