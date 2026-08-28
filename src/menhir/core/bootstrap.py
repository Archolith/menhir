"""Bootstrap helpers for connecting services and services assembly."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from menhir.config import MemorySettings
from menhir.core.runtime_preflight import RuntimeCapabilities
from menhir.infrastructure import GraphitiClient, LLMAdapter, MemoryGraphAdapter, Neo4jRepository
from menhir.services import CandidateService, ContextBuilderService, IngestService, RecallService, ScoringService, LifecycleService


class UnavailableGraphitiClient:
    """Sentinel collaborator that reports Graphiti-backed features as unavailable."""

    #: Distinguishes this sentinel from a real client without an isinstance import
    #: at every call site. A real GraphitiClient does not set this, so
    #: ``getattr(client, "available", True)`` is True only for real clients.
    available = False

    def __init__(self, reason: str) -> None:
        self.reason = reason
        self.scheduler_fallback_base_url = ""
        self.scheduler_fallback_embed_base_url = ""
        self.scheduler_fallback_reranker_base_url = ""
        self.llm_client_ref = None
        self.embedder_ref = None
        self.reranker_ref = None

    def _raise(self) -> RuntimeError:
        raise RuntimeError(self.reason)

    async def build_indices_and_constraints(
        self, *, force: bool = False, timeout: int = 15  # noqa: ASYNC109 -- client API
    ) -> None:
        self._raise()

    async def add_episode(self, **kwargs: Any) -> Any:
        self._raise()

    async def search(self, query: str, **kwargs: Any) -> Any:
        self._raise()

    async def search_scored(
        self,
        query: str,
        *,
        num_results: int = 50,
        group_ids: list[str] | None = None,
    ) -> list[tuple[str, str, float]]:
        self._raise()

    async def embed_query(self, query: str) -> list[float]:
        self._raise()

    async def count_similar_by_cosine(
        self,
        query: str,
        *,
        exclude_uuid: str,
        min_cosine: float,
        limit: int = 50,
        group_ids: list[str] | None = None,
    ) -> int:
        self._raise()

    def circuit_breaker_snapshots(self) -> dict[str, dict[str, Any]]:
        return {
            "llm": {"state": "unavailable", "failures": 0},
            "embed": {"state": "unavailable", "failures": 0},
            "reranker": {"state": "unavailable", "failures": 0},
        }

    def embedding_cache_stats(self) -> dict[str, int]:
        return {"hits": 0, "misses": 0, "size": 0}

    async def close(self) -> None:
        return None


class UnavailableLLMAdapter:
    """Sentinel LLM collaborator for degraded startup."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        self.base_url = ""
        self.api_key = ""
        self.chat_model = ""
        self.embed_model = ""
        self.backend = None

    def _raise(self) -> RuntimeError:
        raise RuntimeError(self.reason)

    def chat_model_name(self) -> str:
        return ""

    def embed_model_name(self) -> str:
        return ""

    async def compress_content(self, content: str) -> str | None:
        self._raise()

    async def merge_content(self, existing_content: str, new_context: str) -> str | None:
        self._raise()

    async def confirm_contradiction(
        self,
        *,
        name_a: str,
        content_a: str,
        name_b: str,
        content_b: str,
    ) -> bool | None:
        self._raise()

    async def repair_edge_facts(
        self,
        episode_content: str,
        edges: list[dict[str, str]],
    ) -> list[str | None]:
        self._raise()

    async def classify_shadow_context(
        self,
        episode_body: str,
        candidates: list[dict[str, str]],
    ) -> str | None:
        self._raise()

    async def break_shadow_tie(
        self,
        episode_body: str,
        tied_candidates: list[dict[str, str]],
    ) -> str | None:
        self._raise()


@dataclass
class BuildArtifacts:
    """Container for assembled runtime collaborators."""

    settings: MemorySettings
    capabilities: RuntimeCapabilities | None
    neo4j: Neo4jRepository
    graphiti_client: GraphitiClient | UnavailableGraphitiClient
    llm: LLMAdapter | UnavailableLLMAdapter
    graph_adapter: MemoryGraphAdapter
    ingest_service: IngestService
    recall_service: RecallService
    scoring_service: ScoringService
    lifecycle_service: LifecycleService
    context_builder: ContextBuilderService
    candidate_service: CandidateService


def build_memory_services(
    settings: MemorySettings | None = None,
    *,
    capabilities: RuntimeCapabilities | None = None,
    read_only: bool = False,
) -> BuildArtifacts:
    """Build collaborators for the v1 memory pipeline."""
    settings = settings or MemorySettings.from_env()
    neo4j = Neo4jRepository(
        uri=settings.neo4j_uri,
        database=settings.neo4j_database,
        user=settings.neo4j_user,
        password=settings.neo4j_password,
    )
    graphiti_reason = "Graphiti-backed features are unavailable in the current startup mode."
    llm_reason = "LLM-backed features are unavailable in the current startup mode."
    graphiti_client: GraphitiClient | UnavailableGraphitiClient
    llm: LLMAdapter | UnavailableLLMAdapter
    if capabilities is None or capabilities.reads_ready:
        try:
            graphiti_client = GraphitiClient.from_settings_with_capabilities(
                settings,
                llm_enabled=(capabilities.llm_ready if capabilities is not None else True),
                reranker_enabled=(capabilities.reranker_ready if capabilities is not None else True),
            )
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning("Graphiti client creation failed, degrading: %s", exc)
            graphiti_client = UnavailableGraphitiClient(f"{graphiti_reason} ({exc})")
    else:
        graphiti_client = UnavailableGraphitiClient(graphiti_reason)
    if capabilities is None or capabilities.llm_ready:
        try:
            llm = LLMAdapter.from_settings(settings)
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning("LLM adapter creation failed, degrading: %s", exc)
            llm = UnavailableLLMAdapter(f"{llm_reason} ({exc})")
    else:
        llm = UnavailableLLMAdapter(llm_reason)
    graph_adapter = MemoryGraphAdapter(neo4j=neo4j)
    scoring_service = ScoringService()
    lifecycle_service = LifecycleService(
        graph_adapter=graph_adapter,
        graphiti_client=graphiti_client,
        llm=llm,
        settings=settings,
    )
    ingest_service = IngestService(
        graphiti_client=graphiti_client,
        graph_adapter=graph_adapter,
        llm=llm,
        lifecycle_service=lifecycle_service,
    )
    ingest_service.configure(
        graphiti_add_episode_timeout_s=float(settings.graphiti_add_episode_timeout_seconds),
        graphiti_episode_max_estimated_tokens=int(settings.graphiti_episode_max_estimated_tokens),
        max_llm_calls_per_session_window=settings.max_llm_calls_per_session_window,
        llm_session_window_seconds=settings.llm_session_window_seconds,
        max_llm_calls_per_enrichment_job=settings.max_llm_calls_per_enrichment_job,
        ingest_concurrency=settings.ingest_concurrency,
        record_detailed_revisions=settings.record_detailed_revisions,
        shadow_context_composition=settings.shadow_context_composition,
        shadow_composition_timeout_s=settings.shadow_composition_timeout_s,
        enrichment_enabled=(capabilities.enrichment_ready if capabilities is not None else True),
        enrichment_disabled_reason=(
            "Graphiti/LLM enrichment is unavailable in the current startup mode."
            if capabilities is not None and not capabilities.enrichment_ready
            else ""
        ),
    )
    recall_service = RecallService(
        graphiti_client=graphiti_client,
        graph_adapter=graph_adapter,
        scoring_service=scoring_service,
        ingest_service=ingest_service,
        lifecycle_service=lifecycle_service,
        scalar_view_authority_enabled=settings.personal_memory_scalar_view_authority_enabled,
        scalar_history_enabled=settings.personal_memory_scalar_history_enabled,
        event_history_authority_enabled=settings.personal_memory_event_history_authority_enabled,
        read_only=read_only,
    )
    context_builder = ContextBuilderService(
        recall_service=recall_service,
        graph_adapter=graph_adapter,
        brief_builder_enabled=settings.frontier_brief_builder,
    )
    candidate_service = CandidateService(
        graph_adapter=graph_adapter,
        lifecycle_service=lifecycle_service,
    )
    return BuildArtifacts(
        settings=settings,
        capabilities=capabilities,
        neo4j=neo4j,
        graphiti_client=graphiti_client,
        llm=llm,
        graph_adapter=graph_adapter,
        ingest_service=ingest_service,
        recall_service=recall_service,
        scoring_service=scoring_service,
        lifecycle_service=lifecycle_service,
        context_builder=context_builder,
        candidate_service=candidate_service,
    )


async def prepare_memory_runtime(
    artifacts: BuildArtifacts,
    *,
    force_graphiti: bool = False,
) -> dict[str, object]:
    """Prepare Graphiti and policy schema prerequisites before runtime ingestion."""

    graphiti_ready = bool(getattr(artifacts.capabilities, "graphiti_ready", True))
    # Only build Graphiti indices when the graphiti client is actually usable.
    # ``graphiti_ready`` is derived from Neo4j + embedder, but the client is a
    # sentinel (Unavailable) when the LLM is not ready — calling build on it
    # raises. Gating on the client's own availability lets the server start
    # against Neo4j alone (graph adapter works; Graphiti features degrade)
    # instead of crashing. When a real client is present this is unchanged.
    graphiti_client_available = getattr(artifacts.graphiti_client, "available", True)
    if graphiti_ready and graphiti_client_available:
        await artifacts.graphiti_client.build_indices_and_constraints(force=force_graphiti)
    schema_ready = await asyncio.to_thread(artifacts.graph_adapter.phase_one_schema_ready)

    if schema_ready:
        schema = {
            "status": "ok",
            "queries_executed": 0,
            "failures": [],
            "skipped": True,
        }
    else:
        # These adapter calls are synchronous and can block the event loop on startup.
        bootstrap_result = await asyncio.to_thread(artifacts.graph_adapter.bootstrap_phase_one)
        # Bootstrap success means only that every DDL statement returned without raising. It is
        # not proof that the required schema exists: Neo4j's IF NOT EXISTS can no-op when a
        # same-named ordinary index or wrong-shaped constraint occupies the name. Re-read the
        # exact semantic contract after the repair attempt and fail before any writer starts.
        schema_ready = await asyncio.to_thread(artifacts.graph_adapter.phase_one_schema_ready)
        if not schema_ready:
            failure_detail = "; ".join(str(item) for item in bootstrap_result.failures)
            suffix = f" Bootstrap failures: {failure_detail}" if failure_detail else ""
            raise RuntimeError(
                "Phase-one schema remains absent or wrong-shaped after bootstrap; refusing "
                f"writer startup.{suffix}"
            )
        schema = {
            "status": "ok",
            "queries_executed": bootstrap_result.queries_executed,
            "failures": bootstrap_result.failures,
            "skipped": False,
        }

    # Edge counts feed decay-protection thresholds; they are not required to serve
    # reads. This is a full-graph recount in one transaction, so any single damaged
    # record (e.g. a corrupt property chain) fails the whole statement. Let the
    # server start with stale counts rather than refusing to boot, matching the
    # Graphiti/LLM degradation above.
    try:
        edge_count_sync = await asyncio.to_thread(artifacts.graph_adapter.sync_edge_counts)
        edge_count_sync_error = None
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("Edge count sync failed, degrading: %s", exc)
        edge_count_sync = 0
        edge_count_sync_error = str(exc)
    return {
        # CF-141: reports BOTH conjuncts of the gate above. It previously read
        # `"ok" if graphiti_ready else "skipped"`, so an Unavailable client -- indices not
        # built -- still reported "ok". Latent only because the sole production caller
        # (`core/runtime.py`) discards this dict; it is consumed by tests, which is exactly
        # where a wrong status is most misleading.
        "graphiti": "ok" if (graphiti_ready and graphiti_client_available) else "skipped",
        "schema": schema,
        "edge_count_sync": edge_count_sync,
        "edge_count_sync_error": edge_count_sync_error,
    }
