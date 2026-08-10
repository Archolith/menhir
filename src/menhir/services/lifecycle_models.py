"""Lifecycle and consolidation policy service."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from time import perf_counter
from typing import TYPE_CHECKING, Any, Awaitable, Callable
from uuid import uuid4

# Optional async callback: (processed, total, current_node_name) -> None
ProgressCallback = Callable[[int, int, str], Awaitable[None]]

from menhir.domain.memory_types import get_policy
from menhir.domain.models import FreshnessState, NodeScope
from menhir.domain.namespace import namespace_to_group_ids
from menhir.domain.utils import days_ago
from menhir.config import MemorySettings
from menhir.infrastructure.cypher import Cypher
from menhir.infrastructure.graphiti_client import GraphitiClient
from menhir.infrastructure.llm import LLMAdapter

if TYPE_CHECKING:
    from menhir.core.bootstrap import UnavailableGraphitiClient, UnavailableLLMAdapter
    from menhir.services.correlation_service import CorrelationService
from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
from menhir.infrastructure.pending_actions import PendingActionStore
from menhir.infrastructure.telemetry import record_lifecycle_action, record_memory_revision, record_mcp_event
from menhir.infrastructure.telemetry.store import telemetry_store

logger = logging.getLogger(__name__)

SHARPNESS_PROMOTE_THRESHOLD = 0.5       # M5: raised from 0.3
PERSISTENT_EDGE_PROMOTE_THRESHOLD = 3   # M5: raised from 2
SIMILARITY_CONFLICT_THRESHOLD = 0.85
ORPHAN_MAX_AGE_HOURS = 4.0
CONSOLIDATION_BATCH_SIZE = 10

DECAY_BATCH_SIZE = 10

# F5 demote-with-TTL: grace window for SESSION nodes that fail to promote.
# Retention-favoring default (upper end of 7–14 range). Deletion is irreversible;
# the wider window is the conservative default. See plans/lifecycle-f5-demote-with-ttl-implementation.md.
DEMOTE_TTL_DAYS = 14

# F2 lawful sharpness: genuine cosine similarity floor for neighbor counting.
# Calibrated 2026-07-10 (P3) against the LME `default` namespace (n=150, seed=0): 0.80 is the
# smallest floor that discriminates -- compress(<0.3)=24%, promote(>=0.5)=71%, zero-neighbor(=1.0)=56%.
# 0.75 was a mass-compress cliff (63% compress-eligible); 0.85+ marks ~77%+ of memories "unique".
# See docs/runbooks/sharpness-cosine-floor-calibration.md (re-run recipe + result) and
# plans/lifecycle-f2-lawful-sharpness-implementation.md P3.
SHARPNESS_COSINE_FLOOR = 0.80

# Default thresholds used only for fetch_decay_candidates queries (pre-filter).
# Actual compress/delete decisions are made by MemoryTypePolicy.should_compress/should_delete.
_DEFAULT_COMPRESS_DAYS = 7    # lowest compress_days across all policies (TEMPORAL)
_DEFAULT_COMPRESS_EDGE_COUNT = 5
_DEFAULT_GONE_DAYS = 30       # lowest gone_days across all policies (TEMPORAL)
_DEFAULT_GONE_EDGE_COUNT = 3
_DEFAULT_GONE_SHARPNESS = 0.1


@dataclass(frozen=True)
class ConsolidationResult:
    """Outcome of a consolidation pass."""

    promoted: int
    deleted: int
    conflicts_detected: int
    skipped_pending: int
    orphan_episodes_cleaned: int
    demoted: int = 0


@dataclass(frozen=True)
class DecayResult:
    """Outcome of a decay pass."""

    edge_counts_synced: int
    sharpness_recalculated: int
    compressed: int
    deleted: int
    edges_bridged: int
    orphan_subgraphs_cleaned: int
