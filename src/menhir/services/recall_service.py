"""Recall service facade over focused policy, support, and pipeline modules."""

from __future__ import annotations

import asyncio
import logging
import math
import os
from dataclasses import dataclass, field
from time import perf_counter
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from menhir.services.ingest_service import IngestService

from menhir.domain.models import FreshnessState, NodeScope, ProcessingState
from menhir.domain.truth.kinds import DIVERSITY_FAMILY as _FRONTIER_DIVERSITY_FAMILY
from menhir.domain.recall import (
    CandidateData,
    QueryPreset,
    RecallResult,
    RetrievalScoreKind,
    ScalarAuthorityContributor,
    ScalarAuthorityVerdict,
    ScoredMemory,
    TemporalFact,
)
from menhir.domain.retrieval_tuning import (
    SOURCE_PRIORS,
    CandidateSource,
    RetrievalTuningConfig,
)
from menhir.domain.retrieval_trace_models import (
    AssertionShadowRow,
    AssertionShadowTrace,
    FacetShadowRow,
    FacetShadowTrace,
    RelevanceBreakdown,
    RetrievalTrace,
    ScoringTrace,
    ViewReachability,
)
from menhir.infrastructure.graphiti_client import GraphitiClient
from menhir.services.hybrid_retrieval import FusionLane, hybrid_search, weighted_rrf_multi

if TYPE_CHECKING:
    from menhir.core.bootstrap import UnavailableGraphitiClient
from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
from menhir.infrastructure.telemetry import record_mcp_event
from menhir.domain.utils import days_ago
from menhir.services.scheduler_protocols import LifecycleServiceProtocol
from menhir.services.scoring_service import (
    GRAPHITI_RRF_DUAL_METHOD_MAX,
    MIN_SIMILARITY_THRESHOLD,
    ScoringService,
)
from menhir.domain.git_staleness import BeliefCommitContext, derive_structural_staleness
from menhir.services.change_log_provider import CachedGitChangeLog, ChangeLogProvider
from menhir.infrastructure.paths import repo_root_for_project

logger = logging.getLogger(__name__)


from menhir.services.recall_policies import (
    FILE_LINKED_BASELINE_SIMILARITY,
    PENDING_ENTITY_SIMILARITY,
    _authority_contributors,
    _belief_markers_from_facts,
    _blend_oracle_order,
    _build_temporal_facts,
    _filter_to_current_beliefs,
    _frontier_trace_enabled,
    _oracle_similarity,
    _query_wants_history,
    _repo_path_for,
    _select_candidate_content,
    _staleness_evidence_for,
)

from menhir.services.recall_pipeline import apply_event_history_authority_layer, run_recall
from menhir.services.recall_support import RecallSupportMixin


@dataclass
class RecallService(RecallSupportMixin):
    """Orchestrates the two-phase retrieval pipeline: search -> score -> rank -> update."""

    graphiti_client: GraphitiClient | UnavailableGraphitiClient
    graph_adapter: MemoryGraphAdapter
    scoring_service: ScoringService
    ingest_service: IngestService | None = None
    lifecycle_service: LifecycleServiceProtocol | None = None
    #: Step 7 canary: when True, a current scalar_state View may suppress an older provenance-linked
    #: graph fact for an explicit current-state query (all six authority gates must pass). Default
    #: OFF -> today's behavior (a View never suppresses a fact).
    scalar_view_authority_enabled: bool = False
    #: When True, scalar_history Views are surfaced as advisory context via a dedicated recall lane.
    #: When False, scalar_history Entities are excluded from generic recall (real rollback).
    scalar_history_enabled: bool = False
    #: When True, a recognized conservative first-person event recall query (with a non-None
    #: namespace) surfaces a leading/advisory event-history authority layer. Default OFF -> today's
    #: behavior (no event authority and no event-assertion repository read).
    event_history_authority_enabled: bool = False
    _rehydration_tasks: set[asyncio.Task[None]] = field(default_factory=set)
    _change_log_provider: ChangeLogProvider = field(default_factory=CachedGitChangeLog)

    async def recall(
        self,
        query: str,
        *,
        preset: QueryPreset = QueryPreset.KNOWLEDGE,
        limit: int = 10,
        candidate_k: int = 50,
        context_node_ids: list[str] | None = None,
        include_session: bool = False,
        include_superseded: bool = False,
        wait_for_pending: bool = False,
        pending_wait_timeout_s: float = 3.0,
        file_context: str | None = None,
        file_context_project: str | None = None,
        namespace: str | None = None,
        include_invalidated: bool = False,
        tuning: RetrievalTuningConfig | None = None,
        trace: bool = False,
        update_access: bool = True,
    ) -> RecallResult:
        result = await run_recall(
            self,
            query,
            preset=preset,
            limit=limit,
            candidate_k=candidate_k,
            context_node_ids=context_node_ids,
            include_session=include_session,
            include_superseded=include_superseded,
            wait_for_pending=wait_for_pending,
            pending_wait_timeout_s=pending_wait_timeout_s,
            file_context=file_context,
            file_context_project=file_context_project,
            namespace=namespace,
            include_invalidated=include_invalidated,
            tuning=tuning,
            trace=trace,
            update_access=update_access,
        )
        return await apply_event_history_authority_layer(self, result, query, namespace)
