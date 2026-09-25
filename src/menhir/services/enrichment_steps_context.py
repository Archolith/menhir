"""Per-episode context object and collapse error for the enrichment pipeline.

Extracted verbatim from ``enrichment_steps.py``; the facade module re-exports everything
here so existing ``menhir.services.enrichment_steps`` import sites keep working unchanged.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Callable

from menhir.services.scheduler_protocols import LifecycleServiceProtocol

from menhir.infrastructure import GraphitiClient, MemoryGraphAdapter
from menhir.infrastructure.evidence_publication_intents import EvidencePublicationIntentRepository
from menhir.services.ingest_gate import IngestGate


class CombinedExtractionCollapsedError(RuntimeError):
    """Raised when combined extraction produced a non-empty payload but persisted nothing.

    Distinguishes a genuine empty extraction ("ok thanks" — no memorable content) from
    a collapse where the LLM DID extract entities/edges but Graphiti's resolution dropped
    them all (dangling endpoints, orphan pruning, malformed rows). The message carries the
    substring ``combined_extraction_collapsed`` so ``classify_enrichment_failure`` routes
    it to the retryable path instead of masking it as an empty-extraction success.
    """


# ---------------------------------------------------------------------------
# Context object — carries per-episode state + adapters into step functions
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EnrichmentContext:
    """Immutable bag of per-episode state and adapters passed to pipeline steps."""

    # Per-episode state
    episode_uuid: str
    claimed: dict[str, object]
    started: float
    processing_attempts: int
    # Identity
    worker_id: str
    # Adapters
    graph_adapter: MemoryGraphAdapter
    graphiti_client: GraphitiClient
    lifecycle_service: LifecycleServiceProtocol | None
    llm: Any | None
    # Synchronization — bounded concurrency + per-namespace serialization
    ingest_gate: IngestGate
    # Config
    processing_steps_total: int
    settings_record_revisions: bool
    ready_warning_ms: int
    graphiti_add_episode_timeout_s: float
    graphiti_episode_max_estimated_tokens: int
    # Callback
    get_queue_depth: Callable[[], int]
    # Stage 1 shadow-mode context composition (observe-only; see shadow_context_composition.py
    # and .agent/plans/menhir-context-composition-production-integration.md). Off by default —
    # every EnrichmentContext construction site that predates this field gets the safe default.
    shadow_context_composition: bool = False
    shadow_composition_timeout_s: float = 30.0
    # Registers a fire-and-forget background task so IngestService can track/drain it at
    # shutdown. None (the default) means "don't track" — used by tests that construct
    # EnrichmentContext directly without a real IngestService behind it.
    register_background_task: Callable[[asyncio.Task], None] | None = None
    # Optional, activation-gated publication protocol.  No runtime/bootstrap path constructs this
    # repository yet because the managed tombstone HMAC key ring and created-only Graphiti artifact
    # manifest do not exist.  Tests and a future explicit activation hook can inject it without
    # changing the extraction API; absence preserves the currently deployed path.
    evidence_publication_intents: EvidencePublicationIntentRepository | None = None
    #: Canonical-self binding rollout: "off" (default), "observe" or "enforce". Defaulted so every
    #: construction site predating this field keeps pre-change behavior.
    canonical_self_binding_mode: str = "off"
