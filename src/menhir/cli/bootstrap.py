"""Lightweight service builder for read-only hook operations."""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass

from menhir.config import MemorySettings
from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
from menhir.infrastructure.neo4j import Neo4jRepository

logger = logging.getLogger(__name__)


@dataclass
class HookServices:
    """Services available to the hook CLI.

    ``graph_adapter`` is always populated (fast path — Neo4j only).
    ``context_builder`` is ``None`` when the Graphiti stack is unavailable.
    """

    settings: MemorySettings
    neo4j: Neo4jRepository
    graph_adapter: MemoryGraphAdapter
    context_builder: object | None  # ContextBuilderService or None


def build_hook_services(settings: MemorySettings | None = None) -> HookServices:
    """Build minimal services for the hook CLI.

    Fast path (always): Neo4j → MemoryGraphAdapter
    Full path (best-effort): + GraphitiClient → RecallService → ContextBuilderService
    """
    settings = settings or MemorySettings.from_env()

    neo4j = Neo4jRepository(
        uri=settings.neo4j_uri,
        database=settings.neo4j_database,
        user=settings.neo4j_user,
        password=settings.neo4j_password,
    )
    graph_adapter = MemoryGraphAdapter(neo4j=neo4j)

    # Attempt full path for context recall.
    # Always prefer the direct Graphiti path — the hook is designed to bypass HTTP.
    # Fall back to BackendContextBuilder only if Graphiti is unavailable and a backend URL is configured.
    context_builder = None
    try:
        from menhir.infrastructure.graphiti_client import GraphitiClient
        from menhir.services.context_builder import ContextBuilderService
        from menhir.services.recall_service import RecallService
        from menhir.services.scoring_service import ScoringService

        graphiti_client = GraphitiClient.from_settings(settings)
        scoring_service = ScoringService()
        recall_service = RecallService(
            graphiti_client=graphiti_client,
            graph_adapter=graph_adapter,
            scoring_service=scoring_service,
            ingest_service=None,
            lifecycle_service=None,
            scalar_view_authority_enabled=settings.personal_memory_scalar_view_authority_enabled,
            event_history_authority_enabled=settings.personal_memory_event_history_authority_enabled,
        )
        context_builder = ContextBuilderService(
            recall_service=recall_service,
            graph_adapter=graph_adapter,
            brief_builder_enabled=settings.frontier_brief_builder,
        )
    except Exception:
        logger.debug("Direct recall path unavailable, trying backend", exc_info=True)
        backend_url = (settings.backend_url or "").strip().rstrip("/")
        if backend_url:
            try:
                from menhir.cli._backend_context import BackendContextBuilder

                context_builder = BackendContextBuilder(backend_url, api_key=settings.api_key)
            except Exception:
                logger.debug("Backend context builder also unavailable", exc_info=True)
        if context_builder is None:
            print("menhir hook: full recall unavailable, flagged-only mode", file=sys.stderr)

    return HookServices(
        settings=settings,
        neo4j=neo4j,
        graph_adapter=graph_adapter,
        context_builder=context_builder,
    )
