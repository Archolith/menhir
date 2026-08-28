"""Public lifecycle service composed from focused workflow owners."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from time import perf_counter
from typing import TYPE_CHECKING, Any
from uuid import uuid4

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

from menhir.services.lifecycle_conflicts import LifecycleConflictMixin
from menhir.services.lifecycle_consolidation import LifecycleConsolidationMixin
from menhir.services.lifecycle_decay import LifecycleDecayMixin
from menhir.services.lifecycle_models import (
    CONSOLIDATION_BATCH_SIZE,
    DECAY_BATCH_SIZE,
    DEMOTE_TTL_DAYS,
    ORPHAN_MAX_AGE_HOURS,
    PERSISTENT_EDGE_PROMOTE_THRESHOLD,
    SHARPNESS_COSINE_FLOOR,
    SHARPNESS_PROMOTE_THRESHOLD,
    SIMILARITY_CONFLICT_THRESHOLD,
    ConsolidationResult,
    DecayResult,
    ProgressCallback,
)

@dataclass
class LifecycleService(
    LifecycleConsolidationMixin, LifecycleDecayMixin, LifecycleConflictMixin
):
    """Service for decay/compaction/consolidation workflows."""

    graph_adapter: MemoryGraphAdapter
    graphiti_client: GraphitiClient | UnavailableGraphitiClient
    llm: LLMAdapter | UnavailableLLMAdapter | None = None
    settings: MemorySettings = field(default_factory=MemorySettings)
    pending_actions: PendingActionStore = field(default_factory=PendingActionStore)
    _consolidation_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _decay_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
