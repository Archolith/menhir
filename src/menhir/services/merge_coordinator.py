"""MergeCoordinator -- the durable saga for a destructive entity merge (plan Phase 4).

The legacy path deleted the absorbed node and THEN wrote a best-effort, failure-swallowing telemetry
row. A crash (or a telemetry failure) between those two steps destroyed the node with no durable
snapshot: unrecoverable. This coordinator inverts that order and makes the operation replayable:

    ELIGIBLE   fail-closed policy recheck on current graph state (invariant 7)
    SNAPSHOT   complete, versioned, checksummed state of BOTH identities (invariant: lossless)
    PREPARED   commit the journal row -- WITH the snapshot -- before touching the graph (invariant 3)
    MUTATE     idempotent merge stamped with op_id
    VERIFY     compare the complete observed after-state to the one frozen at PREPARE
    COMMITTED  only on an exact match; anything else is NEEDS_REVIEW (invariant 5)
    RECONCILE  replay any row left PREPARED by a crash; drift is quarantined, never auto-repaired

If PREPARE fails, the graph is NOT mutated -- the merge abstains. That is the whole point: a merge
may only proceed once its recovery snapshot is durable.

Fencing: the journal's unresolved-key index is keyed on the normalized PAIR key, so while a merge is
PREPARED or NEEDS_REVIEW no competing merge of the same pair can be prepared (invariant 14).
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid as uuidlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from menhir.domain import merge_eligibility as me
from menhir.domain import merge_snapshot as ms
from menhir.infrastructure.graph_operations import GraphOperationsJournal
from menhir.services.saga_writer_heartbeat import owned_mutation
from menhir.services.saga_reconcile_outcomes import (
    DRIFTED,
    FAILED,
    REPLAYED,
    SKIP,
    SKIPPED,
    WOULD_MARK_ALREADY_APPLIED,
    WOULD_NEEDS_REVIEW,
    WOULD_REPLAY,
    summarize_outcomes,
)
from menhir.clock import utc_now_iso as _utc_now_iso

logger = logging.getLogger(__name__)

from menhir.services.merge_coordinator_saga import (
    _canonical,
    MERGE_SNAPSHOT_TOO_LARGE,
    MergeCoordinator,
    MergeDrift,
    MergeGraphAdapter,
    merge_state_fingerprint,
    pair_key,
)

__all__ = [
    "MergeCoordinator",
    "MergeDrift",
    "MergeGraphAdapter",
    "MERGE_SNAPSHOT_TOO_LARGE",
    "merge_state_fingerprint",
    "pair_key",
    "me",
]
