"""MetricWriteCoordinator — the cross-store saga for every Metric write (Metric plan A6/C/E).

SQLite and Neo4j cannot share a transaction, so a Metric write is a recoverable saga:

    PREPARED   commit {graph_operations row + metric_receipts row} in ONE SQLite transaction
    MUTATE     idempotent, preconditioned Neo4j write keyed by op_id (ViewRepository.record_metric)
    VERIFY     compare the observed after-state fingerprint to the one frozen at PREPARE
    COMMITTED  only then mark the journal row committed
    RECONCILE  on startup, replay any row left PREPARED by a crash; drift -> NEEDS_REVIEW

Producers never call ``record_metric`` directly: the graph-only primitive cannot write a receipt,
so a direct caller could mint an unreceipted Metric. They go through ``record_telemetry_fold`` /
``record_run_tally`` here, which own both stores.

Everything replay-sensitive is FROZEN into request_json at PREPARE -- the target key, the chosen
metric UUID, the value/signature, the timestamps. A replay re-executes the frozen request; it never
regenerates a UUID or a timestamp, so replaying after a crash converges instead of forking.

Retention (owner-confirmed 2026-07-13): CHAINED ACCUMULATORS. Each receipt records only the delta
rows in (previous_cutoff, cutoff] and chains the prior digest + aggregate, so raw telemetry rows can
be pruned later without making the count or its lineage unverifiable.

This module is the facade for the ``metric_write_coordinator_*`` sibling modules: the shared
protocols, exceptions, and digest/fingerprint helpers live in ``metric_write_coordinator_common``,
the producer entry points and PREPARE step in ``metric_write_coordinator_saga``
(``MetricSagaWriteMixin``), and replay classification plus reconciliation in
``metric_write_coordinator_reconcile`` (``MetricReconcileMixin``). Every moved symbol is
re-exported here so existing import sites keep working unchanged.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from menhir.infrastructure.graph_operations import GraphOperationsJournal
from menhir.infrastructure.metric_receipts import MetricReceiptStore
from menhir.services.metric_write_coordinator_common import (
    ABSENT,
    MetricChainConflict,
    MetricDrift,
    MetricGraphAdapter,
    RunTallyRecorder,
    _canonical,
    _sha256,
    chain_digest,
    state_fingerprint,
)
from menhir.services.metric_write_coordinator_reconcile import MetricReconcileMixin
from menhir.services.metric_write_coordinator_saga import (
    QUERY_VERSION,
    _ALLOWED_SOURCE_TABLES,
    MetricSagaWriteMixin,
)

logger = logging.getLogger(__name__)


@dataclass
class MetricWriteCoordinator(MetricSagaWriteMixin, MetricReconcileMixin):
    """Owns the telemetry sidecar and the graph adapter; the ONLY sanctioned Metric writer."""

    graph_adapter: MetricGraphAdapter
    journal: GraphOperationsJournal
    receipts: MetricReceiptStore
    telemetry_db_path: Any = None  # Path; defaults to the journal's DB (same sidecar file)
    namespace: str = "agent-experience"
    _key_fn: Any = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.telemetry_db_path is None:
            self.telemetry_db_path = self.journal.db_path
        # Ensure BOTH schemas exist up front. latest_for_key(committed_only=True) JOINs
        # graph_operations, and a fold READ can precede the first journal WRITE on a fresh DB --
        # without this the JOIN hits "no such table: graph_operations".
        self.journal._ensure_ready()
        self.receipts._ensure_ready()

    # ------------------------------------------------------------------ key helper
    @staticmethod
    def view_key(namespace: str | None, subject: str, counter: str) -> str:
        """Must match ViewRepository._key exactly -- the Metric's identity across both stores."""
        ns = (namespace or "").strip()
        return f"{ns}::{subject.strip().lower()}::{counter.strip().lower()}"
