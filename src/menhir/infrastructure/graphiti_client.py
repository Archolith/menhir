"""Reusable Graphiti client wrapper for cth.mcp.memory."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from functools import partial
from datetime import datetime
from time import perf_counter
from typing import Any


logger = logging.getLogger(__name__)

try:
    from graphiti_core import Graphiti
    from graphiti_core.cross_encoder.openai_reranker_client import OpenAIRerankerClient
    from graphiti_core.driver.neo4j_driver import Neo4jDriver
    from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig
    from graphiti_core.llm_client.config import LLMConfig
    from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient
except ModuleNotFoundError as exc:  # pragma: no cover - import guard
    Graphiti = Any  # type: ignore[assignment]
    OpenAIRerankerClient = None  # type: ignore[assignment]
    Neo4jDriver = None  # type: ignore[assignment]
    OpenAIEmbedder = None  # type: ignore[assignment]
    OpenAIEmbedderConfig = None  # type: ignore[assignment]
    LLMConfig = None  # type: ignore[assignment]
    OpenAIGenericClient = None  # type: ignore[assignment]
    _GRAPHITI_IMPORT_ERROR = exc
else:
    _GRAPHITI_IMPORT_ERROR = None

from menhir.config import MemorySettings
from menhir.infrastructure.circuit_breaker import CircuitBreaker
from menhir.infrastructure.embedding_cache import get_embedding_cache
from menhir.infrastructure.embedding_dimensions import expected_graphiti_embedding_dimension
from menhir.infrastructure.observability import build_async_openai_client
from menhir.infrastructure.providers import ProviderConfig, reset_client_cache
from menhir.infrastructure.telemetry import record_lifecycle_event

from menhir.infrastructure import graphiti_client_search  # noqa: E402
from menhir.infrastructure.graphiti_client_construction import (  # noqa: E402
    from_settings_with_capabilities as _build_from_settings_with_capabilities,
)
from menhir.infrastructure.graphiti_client_search import (  # noqa: E402
    _is_vector_dimension_mismatch_error,  # re-exported: implementation moved with the search methods
)

from menhir.infrastructure.graphiti_patches import (  # noqa: E402
    _patch_graphiti_adaptive_dedupe,
    _patch_graphiti_dedup_branch_telemetry,
    _patch_graphiti_combined_extraction,
    _patch_graphiti_combined_extraction_models,
    _patch_graphiti_dedupe_resolutions,
    _patch_graphiti_dedup_identity_gate,
    _patch_graphiti_dedup_prompt,
    _patch_graphiti_edge_none_fields,
    _patch_graphiti_entity_record_group_id,
    _patch_graphiti_entity_extraction,
    _patch_graphiti_node_summary_none,
    _patch_graphiti_none_replace,
    _patch_graphiti_openai_generic_client as _patch_graphiti_openai_generic_client_impl,
    _patch_graphiti_prompt_json,
    _patch_graphiti_structural_candidate_isolation,
    _patch_graphiti_summarize,
    _patch_graphiti_untyped_attribute_preservation,
    _safe_to_prompt_json,  # re-exported for test compatibility
)

__all__ = ["GraphitiClient", "_safe_to_prompt_json"]


@contextlib.contextmanager
def _quiet_equivalent_index_errors():
    """Kept for callers/tests; the filter itself is now installed process-wide by
    ``configure_logging`` (see ``logging_config.EquivalentIndexFilter``)."""

    from menhir.infrastructure.logging_config import install_graphiti_equivalent_index_filter

    install_graphiti_equivalent_index_filter()
    yield


def _schedule_client_close(client: Any) -> asyncio.Task | None:
    """Schedule `aclose()` on a replaced OpenAI/httpx client if a loop is running.

    The three base-URL rebind paths are synchronous, so they cannot `await aclose()`. When an
    event loop is running we schedule the async close on it and return the task so the caller
    can hold a strong reference (an unreferenced task can be garbage-collected mid-flight).
    Returns None when there is nothing to close or no loop is running, in which case the caller
    leaves the close out rather than blocking or leaking an un-awaited coroutine.
    """
    if client is None:
        return None
    inner = getattr(client, "_inner", client)
    http_client = getattr(inner, "_client", None)
    if http_client is None:
        return None
    aclose = getattr(http_client, "aclose", None)
    if aclose is None:
        return None
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    return loop.create_task(aclose())


def _patch_graphiti_openai_generic_client(max_request_estimated_tokens: int | None = None) -> None:
    """Patch Graphiti's OpenAI-compatible client to handle loose JSON output."""
    _patch_graphiti_openai_generic_client_impl(
        OpenAIGenericClient, max_request_estimated_tokens=max_request_estimated_tokens
    )


def _apply_graphiti_construction_patches(settings: MemorySettings) -> None:
    """Apply the Graphiti startup patch sequence in construction order.

    Lives (and stays) in this module so the patch calls keep resolving from these module
    globals; ``graphiti_client_construction.from_settings_with_capabilities`` invokes it
    through this module at the same point in the startup sequence as before the split.
    """

    _patch_graphiti_prompt_json()
    _patch_graphiti_combined_extraction()
    _patch_graphiti_combined_extraction_models()
    _patch_graphiti_entity_extraction()
    _patch_graphiti_dedupe_resolutions()
    _patch_graphiti_dedup_prompt()
    _patch_graphiti_dedup_identity_gate()
    _patch_graphiti_structural_candidate_isolation()
    _patch_graphiti_untyped_attribute_preservation()
    _patch_graphiti_dedup_branch_telemetry()
    _patch_graphiti_adaptive_dedupe()
    _patch_graphiti_openai_generic_client(
        max_request_estimated_tokens=int(settings.graphiti_request_max_estimated_tokens)
    )
    _patch_graphiti_summarize()
    _patch_graphiti_none_replace()
    _patch_graphiti_entity_record_group_id()
    _patch_graphiti_node_summary_none()
    _patch_graphiti_edge_none_fields()


@dataclass
class GraphitiClient:
    """Thin wrapper around a configured Graphiti client instance."""

    client: Graphiti
    llm_base_url: str = ""
    embed_base_url: str = ""
    reranker_base_url: str = ""
    llm_provider_kind: str = ""
    embed_provider_kind: str = ""
    reranker_provider_kind: str = ""
    llm_client_ref: Any | None = field(default=None, repr=False)
    embedder_ref: Any | None = field(default=None, repr=False)
    reranker_ref: Any | None = field(default=None, repr=False)
    embedding_cache: Any | None = field(default=None, repr=False)
    _indices_ready: bool = field(default=False, init=False, repr=False)
    _llm_breaker: CircuitBreaker = field(default=None, init=False, repr=False)  # type: ignore[assignment]
    _embed_breaker: CircuitBreaker = field(default=None, init=False, repr=False)  # type: ignore[assignment]
    _reranker_breaker: CircuitBreaker = field(default=None, init=False, repr=False)  # type: ignore[assignment]
    _pending_client_closes: list[asyncio.Task] = field(default_factory=list, init=False, repr=False)

    def __post_init__(self) -> None:
        self._llm_breaker = CircuitBreaker(name=f"llm:{self.llm_base_url or 'default'}")
        self._embed_breaker = CircuitBreaker(name=f"embed:{self.embed_base_url or 'default'}")
        self._reranker_breaker = CircuitBreaker(name=f"reranker:{self.reranker_base_url or 'default'}")

    def _referenced_clients(self) -> list[Any]:
        """Clients still held by live llm/embed/reranker refs, for close-safety checks."""
        clients: list[Any] = []
        for ref in (self.llm_client_ref, self.embedder_ref, self.reranker_ref):
            if ref is None:
                continue
            client = getattr(ref, "client", None)
            if client is not None:
                clients.append(client)
        return clients

    def _schedule_close_replaced(self, old: Any) -> None:
        """Close a client being replaced by a rebind, unless a sibling ref still uses it.

        llm/embed/reranker share one client when their base URLs match, so an old client may
        still be the live client of a sibling ref. Closing it there would strand that sibling
        on a closed connection pool, so we skip clients that remain referenced.
        """
        if old is None:
            return
        if any(client is old for client in self._referenced_clients()):
            return
        task = _schedule_client_close(old)
        if task is not None:
            # Prune finished tasks first: rebinds happen on every endpoint rotation, so an
            # append-only list is a slow leak of completed task objects in a long-lived process.
            self._pending_client_closes = [
                t for t in self._pending_client_closes if not t.done()
            ]
            self._pending_client_closes.append(task)

    def _reset_and_close_cached_chat_clients(self) -> None:
        """Evict the shared chat-client cache on a rebind AND close what was evicted.

        `reset_client_cache` returns the clients it dropped precisely so they can be closed
        here: clearing the dict alone would trade CF-177's per-call construction for a pool
        leak on every rotation, which is CF-161's defect wearing a different hat.
        """
        for evicted in reset_client_cache():
            self._schedule_close_replaced(evicted)

    @classmethod
    def from_settings(cls, settings: MemorySettings) -> "GraphitiClient":
        """Build a Graphiti client from runtime settings."""
        return cls.from_settings_with_capabilities(settings)

    @classmethod
    def from_settings_with_capabilities(
        cls,
        settings: MemorySettings,
        *,
        llm_enabled: bool = True,
        reranker_enabled: bool = True,
    ) -> "GraphitiClient":
        """Build a Graphiti client from runtime settings with optional degraded collaborators."""

        return _build_from_settings_with_capabilities(
            cls, settings, llm_enabled=llm_enabled, reranker_enabled=reranker_enabled
        )

    async def _await_add_episode_request(self, *, awaitable: Any) -> Any:
        """Await the Graphiti request, cancelling the inner task if the outer timeout fires."""
        pending = asyncio.create_task(awaitable)
        try:
            return await pending
        except BaseException:
            # Ensure the underlying OpenAI/HTTP task is cancelled when the
            # outer asyncio.wait_for timeout fires (CancelledError injection).
            # Without this, the create_task() runs as an orphan after the
            # timeout, holding connections and emitting unhandled-exception
            # warnings when it eventually completes or fails.
            if not pending.done():
                pending.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await pending
            raise


    async def _count_existing_indices(self) -> int:
        """Query Neo4j for current index count."""
        try:
            result = await self.client.driver.execute_query("SHOW INDEXES YIELD name RETURN count(*) AS cnt")
            return result.records[0]["cnt"] if result.records else 0
        except (OSError, RuntimeError, AttributeError) as exc:
            logger.debug("Could not count existing indices: %s", exc)
            return -1

    async def build_indices_and_constraints(
        self, *, force: bool = False, timeout: int = 15  # noqa: ASYNC109 -- bounds wait_for
    ) -> None:
        """Initialize Graphiti storage prerequisites once per client instance.

        Graphiti's Neo4j driver constructor schedules build_indices_and_constraints()
        as a background task. Calling it again can deadlock or stall on connection
        pool contention. We apply a short timeout — if it hangs, the background task
        from the constructor is likely handling it already.
        """

        if self._indices_ready and not force:
            return

        pre_count = await self._count_existing_indices()
        logger.info("build_indices_and_constraints starting (existing indices: %s)", pre_count)

        try:
            with _quiet_equivalent_index_errors():
                await asyncio.wait_for(self.client.build_indices_and_constraints(), timeout=timeout)
            post_count = await self._count_existing_indices()
            logger.info("build_indices_and_constraints completed (indices now: %s)", post_count)
        except asyncio.TimeoutError:
            post_count = await self._count_existing_indices()
            logger.warning(
                "build_indices_and_constraints timed out after %ds (indices now: %s) — "
                "assuming Graphiti background init is handling it",
                timeout,
                post_count,
            )
        self._indices_ready = True

    async def add_episode(
        self,
        *,
        name: str,
        episode_body: str,
        source_description: str,
        reference_time: datetime,
        episode_uuid: str | None = None,
        attempt: int = 1,
        group_id: str = "",
    ) -> Any:
        """Delegate episode ingestion to Graphiti."""
        from menhir.infrastructure.graphiti_extraction_patches import (
            _extract_nodes_combined_for_add_episode,
            get_extraction_receipt,
        )

        receipt = get_extraction_receipt()
        if receipt is not None and str(receipt.self_bind_mode) == "enforce":
            import graphiti_core.graphiti as graphiti_module
            import graphiti_core.utils.maintenance.node_operations as node_operations

            # A failed compatibility patch must not reopen probabilistic self resolution.
            # Check before the native add_episode call can perform any persistence.
            if (
                graphiti_module.extract_nodes is not _extract_nodes_combined_for_add_episode
                or not getattr(node_operations, "_menhir_adaptive_dedupe_patched", False)
                or graphiti_module.resolve_extracted_nodes is not node_operations.resolve_extracted_nodes
            ):
                raise RuntimeError("canonical-self enforce requires combined extraction and resolver bypass")
        task = "memory: graphiti add_episode"
        child_task_id = f"{(episode_uuid or name).replace('-', '')[:8]}:graphiti:add-episode:{int(perf_counter() * 1000)}"
        request_started = perf_counter()
        logger.debug("Graphiti add_episode request begin name=%s", name)
        await asyncio.to_thread(
            partial(
                record_lifecycle_event,
                component="graphiti_client",
                event="add_episode_request",
                state="started",
                episode_uuid=episode_uuid,
                details={"name": name, "task": task, "child_task_id": child_task_id},
            )
        )
        # graphiti_core.Graphiti.add_episode() does not currently accept a caller-supplied
        # external episode ID. We use episode_uuid for telemetry correlation here, then
        # reconcile by anchor name if a local timeout may have hidden a remote success.
        result = await self._llm_breaker.call(
            lambda: self._await_add_episode_request(
                awaitable=self.client.add_episode(
                    name=name,
                    episode_body=episode_body,
                    source_description=source_description,
                    reference_time=reference_time,
                    group_id=group_id,
                ),
            )
        )
        logger.debug(
            "Graphiti add_episode request complete name=%s duration_ms=%s",
            name,
            int((perf_counter() - request_started) * 1000),
        )
        await asyncio.to_thread(
            partial(
                record_lifecycle_event,
                component="graphiti_client",
                event="add_episode_request",
                state="completed",
                episode_uuid=episode_uuid,
                details={
                    "name": name,
                    "duration_ms": int((perf_counter() - request_started) * 1000),
                    "task": task,
                    "child_task_id": child_task_id,
                },
            )
        )
        return result

    async def search(self, query: str, **kwargs: Any) -> Any:
        """Delegate graph search to Graphiti."""

        return await self.client.search(query, **kwargs)

    async def embed_query(self, query: str) -> list[float]:
        """Embed a retrieval query with Graphiti's already-resolved embedder."""
        if self.embedder_ref is None:
            raise RuntimeError("Graphiti embedder is unavailable")
        vector = await self._embed_breaker.call(
            lambda: self.embedder_ref.create(query)
        )
        return [float(value) for value in vector]

    # Search implementations live in graphiti_client_search and are bound onto the class as
    # methods here, which preserves their signatures, docstrings and behavior exactly.
    search_scored = graphiti_client_search.search_scored

    count_similar_by_cosine = graphiti_client_search.count_similar_by_cosine

    search_edges_scored = graphiti_client_search.search_edges_scored

    search_ranked_by_method = graphiti_client_search.search_ranked_by_method

    def circuit_breaker_snapshots(self) -> dict[str, dict[str, Any]]:
        """Return state snapshots for all circuit breakers."""

        return {
            "llm": self._llm_breaker.state_snapshot(),
            "embed": self._embed_breaker.state_snapshot(),
            "reranker": self._reranker_breaker.state_snapshot(),
        }

    def embedding_cache_stats(self) -> dict[str, int]:
        """Return embedding cache hit/miss/size stats."""

        return get_embedding_cache().stats()

    async def close(self) -> None:
        """Close the underlying Graphiti client."""

        await self.client.close()
