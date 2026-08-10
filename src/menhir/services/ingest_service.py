"""Public ingest service composed from queue, worker, and intake owners."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from time import monotonic, perf_counter
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import httpx

from menhir.domain import IngestResult, IngestStatus, MemorySession
from menhir.domain.models import ProcessingState
from menhir.infrastructure import GraphitiClient, LLMAdapter, MemoryGraphAdapter
from menhir.infrastructure.git_log import current_head
from menhir.infrastructure.paths import repo_root_for_project

if TYPE_CHECKING:
    from menhir.core.bootstrap import (
        UnavailableGraphitiClient,
        UnavailableLLMAdapter,
    )
from menhir.services.scheduler_protocols import LifecycleServiceProtocol
from menhir.infrastructure.observability import (
    LLMUsageEvent,
    reset_llm_usage_callback,
    set_llm_usage_callback,
)
from menhir.infrastructure.scheduler_trace import (
    build_episode_scheduler_task,
)
from menhir.infrastructure.telemetry import (
    record_episode_task_event,
    record_lifecycle_event,
)
from menhir.domain.utils import source_confidence_for
from menhir.infrastructure.circuit_breaker import CircuitOpenError
from menhir.infrastructure.graphiti_patches import clear_extraction_receipt
from menhir.services.enrichment_steps import (
    EnrichmentContext,
    add_episode_with_timeout,
    build_episode_preflight_rejection,
    handle_enrichment_failure,
    run_graphiti_extraction,
    run_preflight_rejection,
    stamp_and_finalize,
    try_reconcile_existing,
)
from menhir.services.ingest_gate import IngestGate

logger = logging.getLogger(__name__)

from menhir.services.ingest_intake import IngestIntakeMixin
from menhir.services.ingest_models import _belief_commit_context, _parse_occurred_at
from menhir.services.ingest_queue import IngestQueueMixin
from menhir.services.ingest_worker import IngestWorkerMixin

@dataclass
class IngestService(IngestQueueMixin, IngestWorkerMixin, IngestIntakeMixin):
    """Service responsible for taking raw episodes and extracting memory nodes."""

    graphiti_client: GraphitiClient | UnavailableGraphitiClient
    graph_adapter: MemoryGraphAdapter
    llm: LLMAdapter | UnavailableLLMAdapter
    lifecycle_service: LifecycleServiceProtocol | None = None
    _ingest_concurrency: int = field(default=1, init=False, repr=False)
    _ingest_gate_obj: IngestGate | None = field(default=None, init=False, repr=False)
    _pending_queue: asyncio.Queue[str] | None = field(
        default=None, init=False, repr=False
    )
    _worker_tasks: list[asyncio.Task[None]] = field(
        default_factory=list, init=False, repr=False
    )
    _queued_episode_ids: set[str] = field(default_factory=set, init=False, repr=False)
    _max_enrichment_attempts: int = field(default=3, init=False, repr=False)
    _context_window_retry_attempts: int = field(default=6, init=False, repr=False)
    _enrichment_lease_seconds: int = field(default=900, init=False, repr=False)
    _lease_recovery_poll_s: float = field(default=5.0, init=False, repr=False)
    _queue_warning_depth: int = field(default=5, init=False, repr=False)
    _ready_warning_ms: int = field(default=8000, init=False, repr=False)
    _failed_enrichments: int = field(default=0, init=False, repr=False)
    _worker_id: str = field(
        default_factory=lambda: str(uuid4()), init=False, repr=False
    )
    _processing_steps_total: int = field(default=5, init=False, repr=False)
    _processing_heartbeat_interval_s: float = field(default=5.0, init=False, repr=False)
    _graphiti_add_episode_timeout_s: float = field(
        default=300.0, init=False, repr=False
    )
    _graphiti_episode_max_estimated_tokens: int = field(
        default=12000, init=False, repr=False
    )
    _scheduler_http_client: httpx.AsyncClient | None = field(
        default=None, init=False, repr=False
    )
    # M6 Phase 6 — LLM budget caps
    _session_llm_call_times: dict[str, deque[float]] = field(
        default_factory=dict, init=False, repr=False
    )
    _session_llm_budget_lock: asyncio.Lock = field(
        default_factory=asyncio.Lock, init=False, repr=False
    )
    _budget_settings_max_calls: int = field(default=50, init=False, repr=False)
    _budget_settings_window_s: int = field(default=900, init=False, repr=False)
    _budget_settings_max_per_job: int = field(default=10, init=False, repr=False)
    _job_llm_call_counts: dict[str, int] = field(
        default_factory=dict, init=False, repr=False
    )
    # M6 Phase 5 — sidecar revisions
    _settings_record_revisions: bool = field(default=True, init=False, repr=False)
    _enrichment_enabled: bool = field(default=True, init=False, repr=False)
    _enrichment_disabled_reason: str = field(default="", init=False, repr=False)
    # Stage 1 shadow-mode context composition (observe-only; see
    # .agent/plans/menhir-context-composition-production-integration.md). Off by default.
    _shadow_context_composition: bool = field(default=False, init=False, repr=False)
    _shadow_composition_timeout_s: float = field(default=30.0, init=False, repr=False)
    # Detached background shadow-composition tasks, tracked so shutdown() can drain them
    # instead of leaving "Task was destroyed but it is pending" warnings behind.
    _shadow_tasks: set[asyncio.Task] = field(default_factory=set, init=False, repr=False)
