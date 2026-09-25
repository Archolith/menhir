"""Adapter layer that hides graph-storage internals from policy services.

This module is a thin façade: all operations are delegated to focused
sub-repositories defined in the neighbouring modules.

Sub-repositories:
- ``EpisodeRepository``       — episode_repository.py
- ``ConsolidationRepository`` — consolidation_queries.py
- ``MemoryQueryRepository``   — memory_queries.py
"""

from __future__ import annotations

import logging

from dataclasses import dataclass

from menhir.infrastructure.consolidation_queries import ConsolidationRepository
from menhir.infrastructure.correlation_queries import CorrelationRepository
from menhir.infrastructure.episode_repository import (
    EpisodeRepository,
    PolicyStampResult,
    is_context_window_error_text,
    is_recoverable_context_window_error,
)
from menhir.infrastructure.memory_graph_adapter_anchoring import MemoryGraphAnchoringMixin
from menhir.infrastructure.memory_graph_adapter_candidates import MemoryGraphCandidatesMixin
from menhir.infrastructure.memory_graph_adapter_consolidation import MemoryGraphConsolidationMixin
from menhir.infrastructure.memory_graph_adapter_correlation import MemoryGraphCorrelationMixin
from menhir.infrastructure.memory_graph_adapter_episode_ops import MemoryGraphEpisodeOpsMixin
from menhir.infrastructure.memory_graph_adapter_episodes import MemoryGraphEpisodesMixin
from menhir.infrastructure.memory_graph_adapter_l4_artifacts import MemoryGraphL4ArtifactsMixin
from menhir.infrastructure.memory_graph_adapter_memory_queries import MemoryGraphMemoryQueriesMixin
from menhir.infrastructure.memory_graph_adapter_perception import MemoryGraphPerceptionMixin
from menhir.infrastructure.memory_graph_adapter_schema import (
    MemoryGraphSchemaMixin,
    PhaseOneSchemaResult,
)
from menhir.infrastructure.memory_graph_adapter_structure import MemoryGraphStructureMixin
from menhir.infrastructure.memory_graph_adapter_temporal import MemoryGraphTemporalMixin
from menhir.infrastructure.memory_graph_adapter_todos import MemoryGraphTodosMixin
from menhir.infrastructure.memory_graph_adapter_views import MemoryGraphViewsMixin
from menhir.infrastructure.memory_graph_adapter_work_artifacts import (
    MemoryGraphWorkArtifactsMixin,
)
from menhir.infrastructure.neo4j import Neo4jRepository

logger = logging.getLogger(__name__)

# Re-export so existing callers importing from this module still work.
__all__ = [
    "MemoryGraphAdapter",
    "PhaseOneSchemaResult",
    "PolicyStampResult",
    "is_context_window_error_text",
    "is_recoverable_context_window_error",
    "MemoryGraphAnchoringMixin",
    "MemoryGraphCandidatesMixin",
    "MemoryGraphConsolidationMixin",
    "MemoryGraphCorrelationMixin",
    "MemoryGraphEpisodeOpsMixin",
    "MemoryGraphEpisodesMixin",
    "MemoryGraphL4ArtifactsMixin",
    "MemoryGraphMemoryQueriesMixin",
    "MemoryGraphPerceptionMixin",
    "MemoryGraphSchemaMixin",
    "MemoryGraphStructureMixin",
    "MemoryGraphTemporalMixin",
    "MemoryGraphTodosMixin",
    "MemoryGraphViewsMixin",
    "MemoryGraphWorkArtifactsMixin",
]


@dataclass
class MemoryGraphAdapter(
    MemoryGraphSchemaMixin,
    MemoryGraphEpisodesMixin,
    MemoryGraphMemoryQueriesMixin,
    MemoryGraphConsolidationMixin,
    MemoryGraphEpisodeOpsMixin,
    MemoryGraphStructureMixin,
    MemoryGraphPerceptionMixin,
    MemoryGraphAnchoringMixin,
    MemoryGraphTodosMixin,
    MemoryGraphWorkArtifactsMixin,
    MemoryGraphTemporalMixin,
    MemoryGraphCandidatesMixin,
    MemoryGraphL4ArtifactsMixin,
    MemoryGraphViewsMixin,
    MemoryGraphCorrelationMixin,
):
    """Thin façade over graph storage; delegates to focused sub-repositories."""

    neo4j: Neo4jRepository

    #: The structure query types this adapter will dispatch (CF-164).
    #:
    #: An ALLOWLIST rather than `getattr(self._structure, f"query_{query_type}")` on the caller's
    #: string. That form exposed every `query_*` method the repository happens to define -- 14 of
    #: them against 13 advertised -- so `contained_repos` and `linked_memories` were reachable and
    #: undocumented. `query_linked_memories` is CF-126's unscoped recall, which made that finding
    #: reachable through a type the boundary does not describe.
    #:
    #: The exposure is not the MCP tool, which dispatches literals through its own `if` chain. It
    #: is `POST /api/internal/backend/query_structure`: `query_structure` is in `_BACKEND_METHODS`
    #: (`api/routes_support.py:601`) and falls to the readonly remainder, and
    #: `backend_runtime_data_ops.query_structure` passes `query_type` straight through.
    #:
    #: Three advertised types are absent because they never reach here -- `projects`,
    #: `orphan_structure_projects` and `documents` are answered upstream in
    #: `backend_runtime_data_ops.query_structure` before this fallthrough.
    STRUCTURE_QUERY_TYPES: frozenset[str] = frozenset({
        "overview",
        "files",
        "imports",
        "tests",
        "endpoints",
        "dependencies",
        "cross_refs",
        "blast_radius",
        "affected_tests",
        "symbols",
        "context",
    })

    def __post_init__(self) -> None:
        from menhir.infrastructure.memory_queries import MemoryQueryRepository
        from menhir.infrastructure.structure_queries import StructureGraphWriter
        from menhir.infrastructure.todo_repository import TodoRepository
        from menhir.infrastructure.temporal_repository import TemporalRepository
        from menhir.infrastructure.candidate_repository import CandidateRepository
        from menhir.infrastructure.artifact_repository import ArtifactRepository
        from menhir.infrastructure.view_repository import ViewRepository
        from menhir.infrastructure.work_artifact_repository import WorkArtifactRepository
        from menhir.infrastructure.personal_memory_queries import PersonalMemoryRepository
        from menhir.infrastructure.turn_evidence_repository import TurnEvidenceRepository

        self._memory_queries = MemoryQueryRepository(self.neo4j)
        self._episodes = EpisodeRepository(self.neo4j)
        self._consolidation = ConsolidationRepository(self.neo4j)
        self._correlation = CorrelationRepository(self.neo4j)
        self._structure = StructureGraphWriter(self.neo4j)
        self._todos = TodoRepository(self.neo4j)
        self._temporal = TemporalRepository(self.neo4j)
        self._candidates = CandidateRepository(self.neo4j)
        # _artifacts is the L4 institutional loop (Decision/Failure/Incident);
        # _work_artifacts is the engineering-document model. Different classes
        # answering different questions -- see domain/work_artifact.py.
        self._artifacts = ArtifactRepository(self.neo4j)
        self._work_artifacts = WorkArtifactRepository(self.neo4j)
        self._views = ViewRepository(self.neo4j)
        self._personal_memory = PersonalMemoryRepository(self.neo4j)
        self._turn_evidence = TurnEvidenceRepository(self.neo4j)
        from menhir.infrastructure.tool_event_repository import ToolEventRepository
        self._tool_events = ToolEventRepository(self.neo4j)
        from menhir.infrastructure.typed_assertion_repository import TypedAssertionRepository
        self._typed_assertions = TypedAssertionRepository(self.neo4j)
        from menhir.infrastructure.typed_event_repository import TypedEventAssertionRepository
        self._typed_event_assertions = TypedEventAssertionRepository(self.neo4j)
