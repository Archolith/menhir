"""Cross-database saga journal for SQLite-snapshot + Neo4j-mutate operations.

SQLite and Neo4j cannot share a transaction, so every operation that snapshots to the
sidecar and then mutates the graph runs as a recoverable saga:

    PREPARED  -> commit a journal row (op_id, request, snapshot) in SQLite
    MUTATE    -> idempotent, preconditioned Neo4j write keyed by op_id
    COMMITTED -> mark the journal row committed only after the after-state verifies
    RECONCILE -> replay any row still PREPARED after a crash; drift -> NEEDS_REVIEW

The missing durable-before-delete record is exactly why ~24 nodes destroyed by the
degree-zero orphan cleanup on 2026-07-12 were unrecoverable. This journal makes every
Metric write, migration, and reversal replayable and reversible. See the workspace-root
artifact .agent/plans/menhir-metric-provenance-redesign.md (Part E).

Invariants enforced here (Part E1):
  - operation identity and request_json are immutable once PREPARED is committed;
  - at most one PREPARED routine METRIC_WRITE may exist per target_key (fencing);
  - a NEEDS_REVIEW row never transitions except by an explicit operator command.

Per-participant fencing (invariant 14): entity-pair and delete operations additionally take one
lock per participant UUID in ``graph_operation_locks``, so two unresolved operations can never
share a node even when their pair keys differ (a merge of A+B vs a merge of B+C, or a delete of B
vs a merge of A+B). The lock releases only on a terminal state or an audited NEEDS_REVIEW clearance.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import uuid as uuidlib
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from menhir.infrastructure import operation_owner as oo
from menhir.infrastructure.telemetry import connect_telemetry_db, default_telemetry_db_path
from menhir.clock import utc_now_iso as _utc_now_iso
from menhir.infrastructure.graph_operations_model import (
    OPERATION_KINDS,
    OPERATION_STATES,
    RECONCILIATION_LEASE_NAME,
    GraphOperationError,
    SagaWritesPausedError,
)
from menhir.infrastructure.graph_operations_ownership import _GraphOperationsOwnershipMixin
from menhir.infrastructure.graph_operations_prepare import _GraphOperationsPrepareMixin
from menhir.infrastructure.graph_operations_reads import _GraphOperationsReadsMixin
from menhir.infrastructure.graph_operations_transitions import _GraphOperationsTransitionsMixin

logger = logging.getLogger(__name__)


@dataclass
class GraphOperationsJournal(
    _GraphOperationsPrepareMixin,
    _GraphOperationsTransitionsMixin,
    _GraphOperationsOwnershipMixin,
    _GraphOperationsReadsMixin,
):
    """The ``graph_operations`` table: the durable record of every graph mutation.

    Rows are the source of intent; the graph is the source of truth for "done". A row's
    identity (op_id) and request_json are frozen after PREPARED so a replay can never
    silently change what was intended.

    Method groups live in sibling modules (file-size refactor): schema and the PREPARED write
    path in ``graph_operations_prepare``, state transitions in ``graph_operations_transitions``,
    ownership claims in ``graph_operations_ownership``, and reads/lineage in
    ``graph_operations_reads``. Every public name remains importable from this module.
    """

    db_path: Path = field(default_factory=default_telemetry_db_path)
    _initialized: bool = field(default=False, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
