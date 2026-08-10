"""Episode ingestion service."""

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


def _parse_occurred_at(value: str | None) -> datetime | None:
    """Parse an ISO-8601 occurred_at string to a tz-aware datetime, or None on failure."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        # WARNING, not DEBUG: the caller went out of its way to supply a world time and it is being
        # dropped, so the episode gets stamped with ingestion time and will supersede in the wrong
        # order. This fired invisibly at DEBUG for weeks -- see `coerce_reference_time`.
        logger.warning(
            "occurred_at %r is not ISO-8601; falling back to ingestion time. Backdated history "
            "will supersede in the WRONG order.", value,
        )
        return None


def _belief_commit_context(
    project: str | None,
    *,
    head_fn=current_head,
    repo_resolver=repo_root_for_project,
) -> tuple[str | None, str | None]:
    """Best-effort (commit, branch) for a project's repo, or (None, None) on any miss.

    Args:
        project: Project name to resolve.
        head_fn: Function to get current HEAD (defaults to current_head).
        repo_resolver: Function to resolve repo root (defaults to repo_root_for_project).

    Returns:
        Tuple of (commit_sha, branch_name) or (None, None) on any failure.
    """
    if not project:
        return (None, None)
    try:
        root = repo_resolver(project)
        if root is None:
            return (None, None)
        sha, branch = head_fn(str(root))
        return (sha, branch)
    except Exception:
        return (None, None)
