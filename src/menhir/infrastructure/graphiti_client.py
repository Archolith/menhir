"""Reusable Graphiti client wrapper for cth.mcp.memory."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from time import monotonic, perf_counter
from typing import Any

import httpx

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
from menhir.infrastructure.llama_endpoint import (
    acquire_llama_url_async,
    acquire_llama_url_sync,
    scheduler_url_from_env,
    should_use_scheduler,
)
from menhir.infrastructure.observability import build_async_openai_client
from menhir.infrastructure.providers import ProviderConfig
from menhir.infrastructure.scheduler_trace import (
    build_episode_child_details,
    build_episode_scheduler_task,
    emit_scheduler_task_event,
)
from menhir.infrastructure.telemetry import record_lifecycle_event

from menhir.infrastructure.graphiti_patches import (  # noqa: E402
    _patch_graphiti_adaptive_dedupe,
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


def _is_vector_dimension_mismatch_error(exc: Exception) -> bool:
    """Return True when Neo4j/Graphiti failed due to mixed embedding dimensions."""

    text = str(exc).lower()
    return (
        "vector.similarity.cosine" in text
        and "dimension" in text
    ) or "do not have the same number of dimensions" in text


def _patch_graphiti_openai_generic_client(max_request_estimated_tokens: int | None = None) -> None:
    """Patch Graphiti's OpenAI-compatible client to handle loose JSON output."""
    _patch_graphiti_openai_generic_client_impl(
        OpenAIGenericClient, max_request_estimated_tokens=max_request_estimated_tokens
    )


@dataclass
class GraphitiClient:
    """Thin wrapper around a configured Graphiti client instance."""

    client: Graphiti
    scheduler_fallback_base_url: str = ""
    scheduler_fallback_embed_base_url: str = ""
    scheduler_fallback_reranker_base_url: str = ""
    scheduler_api_key: str = ""
    scheduler_embed_api_key: str = ""
    scheduler_reranker_api_key: str = ""
    scheduler_settings: MemorySettings | None = field(default=None, repr=False)
    llm_base_url: str = ""
    embed_base_url: str = ""
    reranker_base_url: str = ""
    llm_provider_kind: str = ""
    embed_provider_kind: str = ""
    reranker_provider_kind: str = ""
    llm_client_ref: Any | None = field(default=None, repr=False)
    embedder_ref: Any | None = field(default=None, repr=False)
    reranker_ref: Any | None = field(default=None, repr=False)
    _indices_ready: bool = field(default=False, init=False, repr=False)
    scheduler_request_stall_timeout_s: float = field(default=45.0, repr=False)
    _llm_breaker: CircuitBreaker = field(default=None, init=False, repr=False)  # type: ignore[assignment]
    _embed_breaker: CircuitBreaker = field(default=None, init=False, repr=False)  # type: ignore[assignment]
    _reranker_breaker: CircuitBreaker = field(default=None, init=False, repr=False)  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self._llm_breaker = CircuitBreaker(name=f"llm:{self.llm_base_url or 'default'}")
        self._embed_breaker = CircuitBreaker(name=f"embed:{self.embed_base_url or 'default'}")
        self._reranker_breaker = CircuitBreaker(name=f"reranker:{self.reranker_base_url or 'default'}")

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

        if _GRAPHITI_IMPORT_ERROR is not None:
            raise ModuleNotFoundError(
                "graphiti_core is required to construct a GraphitiClient from settings."
            ) from _GRAPHITI_IMPORT_ERROR

        _patch_graphiti_prompt_json()
        _patch_graphiti_combined_extraction()
        _patch_graphiti_combined_extraction_models()
        _patch_graphiti_entity_extraction()
        _patch_graphiti_dedupe_resolutions()
        _patch_graphiti_dedup_prompt()
        _patch_graphiti_dedup_identity_gate()
        _patch_graphiti_structural_candidate_isolation()
        _patch_graphiti_untyped_attribute_preservation()
        _patch_graphiti_adaptive_dedupe()
        _patch_graphiti_openai_generic_client(
            max_request_estimated_tokens=int(settings.graphiti_request_max_estimated_tokens)
        )
        _patch_graphiti_summarize()
        _patch_graphiti_none_replace()
        _patch_graphiti_entity_record_group_id()
        _patch_graphiti_node_summary_none()
        _patch_graphiti_edge_none_fields()
        llm_provider = ProviderConfig.for_graphiti_llm(settings)
        embed_provider = ProviderConfig.for_graphiti_embedder(settings)
        reranker_provider = ProviderConfig.for_graphiti_reranker(settings)
        # Make the effective provider resolution visible at startup. Provider config is
        # read straight from the environment, so an inherited/ambient var can silently
        # override the intended config; log the resolved chain (never the api keys) so a
        # misconfiguration is obvious from the logs instead of requiring a probe.
        logger.info(
            "Graphiti providers resolved: llm=%s base=%s model=%s | embed=%s base=%s model=%s | reranker=%s base=%s",
            llm_provider.kind, llm_provider.base_url, llm_provider.chat_model,
            embed_provider.kind, embed_provider.base_url, embed_provider.embed_model,
            reranker_provider.kind, reranker_provider.base_url,
        )
        if not llm_provider.supports_graphiti_openai_contract():
            raise NotImplementedError(
                "Graphiti provider must currently be openai_compat or openai. "
                "Gemini and Anthropic require a dedicated Graphiti bridge."
            )
        if not embed_provider.supports_graphiti_openai_contract():
            raise NotImplementedError(
                "Graphiti embed provider must currently be openai_compat or openai."
            )
        if not reranker_provider.supports_graphiti_openai_contract():
            raise NotImplementedError(
                "Graphiti reranker provider must currently be openai_compat or openai."
            )

        fallback_base_url = llm_provider.base_url
        llama_base_url = (
            acquire_llama_url_sync(
                fallback=fallback_base_url,
                task="memory: graphiti bootstrap",
            )
            if should_use_scheduler(fallback_base_url)
            else fallback_base_url
        )
        _embed_cache = get_embedding_cache()
        async_client = build_async_openai_client(
            base_url=llama_base_url,
            api_key=llm_provider.api_key,
            settings=settings,
            embedding_cache=_embed_cache,
        )

        llm_client = None
        if llm_enabled:
            # Pin temperature=0 for deterministic extraction and dedup.
            # Graphiti's DEFAULT_TEMPERATURE is 1, which permits high sampling
            # variance — live-traced as the root cause of stochastic entity
            # conflation (e.g. "the suburbs" merged into "Chicago" at ~3% rate).
            llm_client = OpenAIGenericClient(
                config=LLMConfig(
                    api_key=llm_provider.api_key,
                    base_url=llama_base_url,
                    model=llm_provider.chat_model,
                    temperature=0,
                ),
                client=async_client,
                max_tokens=settings.llm_max_tokens,
            )
        embed_fallback_base_url = embed_provider.base_url
        embed_base_url = (
            acquire_llama_url_sync(
                fallback=embed_fallback_base_url,
                task="memory: graphiti embed bootstrap",
            )
            if should_use_scheduler(embed_fallback_base_url)
            else embed_fallback_base_url
        )
        embed_dimension = expected_graphiti_embedding_dimension(settings)
        embed_client = (
            async_client
            if embed_base_url == llama_base_url
            else build_async_openai_client(
                base_url=embed_base_url,
                api_key=embed_provider.api_key,
                settings=settings,
                embedding_cache=_embed_cache,
            )
        )
        embedder = OpenAIEmbedder(
            config=OpenAIEmbedderConfig(
                api_key=embed_provider.api_key,
                base_url=embed_base_url,
                embedding_model=embed_provider.embed_model,
                **({"embedding_dim": embed_dimension} if embed_dimension is not None else {}),
            ),
            client=embed_client,
        )
        reranker_fallback_base_url = reranker_provider.base_url
        reranker_base_url = (
            acquire_llama_url_sync(
                fallback=reranker_fallback_base_url,
                task="memory: graphiti reranker bootstrap",
            )
            if should_use_scheduler(reranker_fallback_base_url)
            else reranker_fallback_base_url
        )
        reranker_client = (
            async_client
            if reranker_base_url == llama_base_url
            else build_async_openai_client(
                base_url=reranker_base_url,
                api_key=reranker_provider.api_key,
                settings=settings,
            )
        )
        cross_encoder = None
        if reranker_enabled:
            cross_encoder = OpenAIRerankerClient(
                config=LLMConfig(
                    api_key=reranker_provider.api_key,
                    base_url=reranker_base_url,
                    model=reranker_provider.chat_model,
                ),
                client=reranker_client,
            )
        graph_driver = Neo4jDriver(
            uri=settings.neo4j_uri,
            user=settings.neo4j_user,
            password=settings.neo4j_password,
            database=settings.neo4j_database,
        )
        return cls(
            client=Graphiti(
                uri=settings.neo4j_uri,
                user=settings.neo4j_user,
                password=settings.neo4j_password,
                graph_driver=graph_driver,
                llm_client=llm_client,
                embedder=embedder,
                cross_encoder=cross_encoder,
            ),
            scheduler_fallback_base_url=fallback_base_url,
            scheduler_fallback_embed_base_url=embed_fallback_base_url,
            scheduler_fallback_reranker_base_url=reranker_fallback_base_url,
            scheduler_api_key=llm_provider.api_key,
            scheduler_embed_api_key=embed_provider.api_key,
            scheduler_reranker_api_key=reranker_provider.api_key,
            scheduler_settings=settings,
            llm_base_url=llama_base_url,
            embed_base_url=embed_base_url,
            reranker_base_url=reranker_base_url,
            llm_provider_kind=llm_provider.kind.value,
            embed_provider_kind=embed_provider.kind.value,
            reranker_provider_kind=reranker_provider.kind.value,
            llm_client_ref=llm_client,
            embedder_ref=embedder,
            reranker_ref=cross_encoder,
            scheduler_request_stall_timeout_s=max(
                5.0,
                float(settings.graphiti_request_stall_timeout_seconds),
            ),
        )

    async def _fetch_scheduler_status(self) -> dict[str, Any] | None:
        url = f"{scheduler_url_from_env().rstrip('/')}/watchdog-status"
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                response = await client.get(url)
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, OSError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    def _uses_scheduler_watchdog(self) -> bool:
        """Return True when the active Graphiti request depends on scheduler-managed endpoints."""

        candidates = (
            self.scheduler_fallback_base_url,
            self.scheduler_fallback_embed_base_url,
            self.scheduler_fallback_reranker_base_url,
            self.llm_base_url,
            self.embed_base_url,
            self.reranker_base_url,
        )
        return any(should_use_scheduler(url) for url in candidates if url)

    async def _await_add_episode_request(
        self,
        *,
        awaitable: Any,
        task: str,
        episode_uuid: str | None,
        child_task_id: str,
    ) -> Any:
        pending = asyncio.create_task(awaitable)
        if not self._uses_scheduler_watchdog():
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

        idle_started_at: float | None = None
        status_missing_started_at: float | None = None
        watchdog_reason: str | None = None
        watchdog_started = False
        try:
            while True:
                done, _ = await asyncio.wait({pending}, timeout=2.0)
                if pending in done:
                    return await pending

                status = await self._fetch_scheduler_status()
                now = monotonic()
                if status is None:
                    if status_missing_started_at is None:
                        status_missing_started_at = now
                        watchdog_reason = "scheduler_status_unavailable"
                        watchdog_started = True
                        record_lifecycle_event(
                            component="graphiti_client",
                            event="add_episode_request_watchdog",
                            state="started",
                            episode_uuid=episode_uuid,
                            details={
                                "task": task,
                                "child_task_id": child_task_id,
                                "reason": watchdog_reason,
                            },
                        )
                        continue
                    stalled_for_s = now - status_missing_started_at
                    if stalled_for_s < self.scheduler_request_stall_timeout_s:
                        continue

                    pending.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await pending
                    message = (
                        "graphiti add_episode stalled while scheduler status was unavailable "
                        f"for {int(stalled_for_s)}s"
                    )
                    record_lifecycle_event(
                        component="graphiti_client",
                        event="add_episode_request_watchdog",
                        state="failed",
                        episode_uuid=episode_uuid,
                        details={
                            "task": task,
                            "child_task_id": child_task_id,
                            "stalled_for_s": int(stalled_for_s),
                            "reason": watchdog_reason,
                        },
                    )
                    raise TimeoutError(message)

                status_missing_started_at = None
                is_active = False
                if isinstance(status, dict):
                    current_task = str(status.get("current_task") or "")
                    is_active = (
                        current_task == task
                        or bool(status.get("slot_active"))
                        or bool(status.get("active_proxy_connections"))
                    )
                if is_active:
                    idle_started_at = None
                    continue

                if idle_started_at is None:
                    idle_started_at = now
                    watchdog_reason = "scheduler_idle"
                    watchdog_started = True
                    record_lifecycle_event(
                        component="graphiti_client",
                        event="add_episode_request_watchdog",
                        state="started",
                        episode_uuid=episode_uuid,
                        details={
                            "task": task,
                            "child_task_id": child_task_id,
                            "reason": watchdog_reason,
                        },
                    )
                    continue
                stalled_for_s = now - idle_started_at
                if stalled_for_s < self.scheduler_request_stall_timeout_s:
                    continue

                pending.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await pending
                message = (
                    "graphiti add_episode stalled after scheduler request went idle "
                    f"for {int(stalled_for_s)}s"
                )
                record_lifecycle_event(
                    component="graphiti_client",
                    event="add_episode_request_watchdog",
                    state="failed",
                    episode_uuid=episode_uuid,
                    details={
                        "task": task,
                        "child_task_id": child_task_id,
                        "stalled_for_s": int(stalled_for_s),
                        "reason": watchdog_reason,
                    },
                )
                raise TimeoutError(message)
        finally:
            if watchdog_started:
                record_lifecycle_event(
                    component="graphiti_client",
                    event="add_episode_request_watchdog",
                    state="completed",
                    episode_uuid=episode_uuid,
                    details={
                        "task": task,
                        "child_task_id": child_task_id,
                        "reason": watchdog_reason,
                    },
                )

    def _maybe_update_client_base_url(self, *, llm_base_url: str) -> None:
        settings = self.scheduler_settings
        if settings is None or not llm_base_url:
            return
        previous_llm_base_url = self.llm_base_url
        embed_tracks_llm = self.embed_base_url == previous_llm_base_url
        if llm_base_url == previous_llm_base_url:
            return

        client = None
        llm_client = self.llm_client_ref
        if llm_client is not None:
            client = build_async_openai_client(
                base_url=llm_base_url,
                api_key=self.scheduler_api_key,
                settings=settings,
            )
            if hasattr(llm_client, "client"):
                llm_client.client = client
            config = getattr(llm_client, "config", None)
            if config is not None and hasattr(config, "base_url"):
                config.base_url = llm_base_url
            self.llm_base_url = llm_base_url

        embedder = self.embedder_ref
        if embedder is None or not embed_tracks_llm:
            return

        if client is not None and hasattr(embedder, "client"):
            embedder.client = client
        embed_config = getattr(embedder, "config", None)
        if embed_config is not None and hasattr(embed_config, "base_url"):
            embed_config.base_url = llm_base_url
        self.embed_base_url = llm_base_url

    def _maybe_update_embed_base_url(self, *, embed_base_url: str) -> None:
        settings = self.scheduler_settings
        if settings is None or not embed_base_url or embed_base_url == self.embed_base_url:
            return

        embedder = self.embedder_ref
        if embedder is None:
            return

        client = build_async_openai_client(
            base_url=embed_base_url,
            api_key=self.scheduler_embed_api_key,
            settings=settings,
        )
        if hasattr(embedder, "client"):
            embedder.client = client
        embed_config = getattr(embedder, "config", None)
        if embed_config is not None and hasattr(embed_config, "base_url"):
            embed_config.base_url = embed_base_url
        self.embed_base_url = embed_base_url

    def _maybe_update_reranker_base_url(self, *, reranker_base_url: str) -> None:
        settings = self.scheduler_settings
        if settings is None or not reranker_base_url or reranker_base_url == self.reranker_base_url:
            return

        reranker = self.reranker_ref
        if reranker is None:
            return

        client = build_async_openai_client(
            base_url=reranker_base_url,
            api_key=self.scheduler_reranker_api_key,
            settings=settings,
        )
        if hasattr(reranker, "client"):
            reranker.client = client
        reranker_config = getattr(reranker, "config", None)
        if reranker_config is not None and hasattr(reranker_config, "base_url"):
            reranker_config.base_url = reranker_base_url
        self.reranker_base_url = reranker_base_url

    async def _ensure_graphiti_endpoints_alive(self, *, task: str) -> None:
        """Wake scheduler-managed OpenAI-compatible endpoints used by Graphiti."""

        logger.debug("Graphiti wake sequence start task=%s", task)
        record_lifecycle_event(
            component="graphiti_client",
            event="wake_sequence",
            state="started",
            details={"task": task},
        )
        llm_fallback = self.scheduler_fallback_base_url
        try:
            if llm_fallback and should_use_scheduler(llm_fallback):
                try:
                    started = perf_counter()
                    logger.debug(
                        "Graphiti endpoint wake start kind=llm task=%s fallback=%s",
                        task,
                        llm_fallback,
                    )
                    record_lifecycle_event(
                        component="graphiti_client",
                        event="wake_llm_endpoint",
                        state="started",
                        details={"task": task, "fallback": llm_fallback},
                    )
                    acquired_url = await acquire_llama_url_async(fallback=llm_fallback, task=task)
                    self._maybe_update_client_base_url(llm_base_url=acquired_url)
                    logger.debug(
                        "Graphiti endpoint wake complete kind=llm task=%s acquired=%s duration_ms=%s",
                        task,
                        acquired_url,
                        int((perf_counter() - started) * 1000),
                    )
                    record_lifecycle_event(
                        component="graphiti_client",
                        event="wake_llm_endpoint",
                        state="completed",
                        details={"task": task, "acquired": acquired_url},
                    )
                except (httpx.HTTPError, OSError, asyncio.TimeoutError, RuntimeError) as exc:
                    logger.warning("scheduler acquire failed for graphiti llm; continuing with fallback endpoint: %s", exc)
                    record_lifecycle_event(
                        component="graphiti_client",
                        event="wake_llm_endpoint",
                        state="failed",
                        details={"task": task, "error": str(exc)},
                    )

            embed_fallback = self.scheduler_fallback_embed_base_url
            if embed_fallback and should_use_scheduler(embed_fallback):
                try:
                    started = perf_counter()
                    embed_task = f"{task} embed"
                    logger.debug(
                        "Graphiti endpoint wake start kind=embed task=%s fallback=%s",
                        embed_task,
                        embed_fallback,
                    )
                    record_lifecycle_event(
                        component="graphiti_client",
                        event="wake_embed_endpoint",
                        state="started",
                        details={"task": embed_task, "fallback": embed_fallback},
                    )
                    acquired_embed_url = await acquire_llama_url_async(
                        fallback=embed_fallback,
                        task=embed_task,
                    )
                    self._maybe_update_embed_base_url(embed_base_url=acquired_embed_url)
                    logger.debug(
                        "Graphiti endpoint wake complete kind=embed task=%s acquired=%s duration_ms=%s",
                        embed_task,
                        acquired_embed_url,
                        int((perf_counter() - started) * 1000),
                    )
                    record_lifecycle_event(
                        component="graphiti_client",
                        event="wake_embed_endpoint",
                        state="completed",
                        details={"task": embed_task, "acquired": acquired_embed_url},
                    )
                except (httpx.HTTPError, OSError, asyncio.TimeoutError, RuntimeError) as exc:
                    logger.warning(
                        "scheduler acquire failed for graphiti embedder; continuing with fallback endpoint: %s",
                        exc,
                    )
                    record_lifecycle_event(
                        component="graphiti_client",
                        event="wake_embed_endpoint",
                        state="failed",
                        details={"task": embed_task, "error": str(exc)},
                    )

            reranker_fallback = self.scheduler_fallback_reranker_base_url
            if reranker_fallback and should_use_scheduler(reranker_fallback):
                try:
                    started = perf_counter()
                    reranker_task = f"{task} reranker"
                    logger.debug(
                        "Graphiti endpoint wake start kind=reranker task=%s fallback=%s",
                        reranker_task,
                        reranker_fallback,
                    )
                    record_lifecycle_event(
                        component="graphiti_client",
                        event="wake_reranker_endpoint",
                        state="started",
                        details={"task": reranker_task, "fallback": reranker_fallback},
                    )
                    acquired_reranker_url = await acquire_llama_url_async(
                        fallback=reranker_fallback,
                        task=reranker_task,
                    )
                    self._maybe_update_reranker_base_url(reranker_base_url=acquired_reranker_url)
                    logger.debug(
                        "Graphiti endpoint wake complete kind=reranker task=%s acquired=%s duration_ms=%s",
                        reranker_task,
                        acquired_reranker_url,
                        int((perf_counter() - started) * 1000),
                    )
                    record_lifecycle_event(
                        component="graphiti_client",
                        event="wake_reranker_endpoint",
                        state="completed",
                        details={"task": reranker_task, "acquired": acquired_reranker_url},
                    )
                except (httpx.HTTPError, OSError, asyncio.TimeoutError, RuntimeError) as exc:
                    logger.warning(
                        "scheduler acquire failed for graphiti reranker; continuing with fallback endpoint: %s",
                        exc,
                    )
                    record_lifecycle_event(
                        component="graphiti_client",
                        event="wake_reranker_endpoint",
                        state="failed",
                        details={"task": reranker_task, "error": str(exc)},
                    )
        finally:
            logger.debug("Graphiti wake sequence complete task=%s", task)
            record_lifecycle_event(
                component="graphiti_client",
                event="wake_sequence",
                state="completed",
                details={"task": task},
            )

    async def _count_existing_indices(self) -> int:
        """Query Neo4j for current index count."""
        try:
            result = await self.client.driver.execute_query("SHOW INDEXES YIELD name RETURN count(*) AS cnt")
            return result.records[0]["cnt"] if result.records else 0
        except (OSError, RuntimeError, AttributeError) as exc:
            logger.debug("Could not count existing indices: %s", exc)
            return -1

    async def build_indices_and_constraints(self, *, force: bool = False, timeout: int = 15) -> None:
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
        uses_scheduler_trace = self._uses_scheduler_watchdog()
        task = (
            build_episode_scheduler_task(
                episode_uuid=episode_uuid,
                provider="graphiti",
                action="add-episode",
            )
            if episode_uuid and uses_scheduler_trace
            else "memory: graphiti add_episode"
        )
        child_task_id = f"{(episode_uuid or name).replace('-', '')[:8]}:graphiti:add-episode:{int(perf_counter() * 1000)}"
        if episode_uuid and uses_scheduler_trace:
            await emit_scheduler_task_event(
                parent_job_id=episode_uuid,
                parent_label=name,
                parent_state="graphiti_extracting",
                child={
                    "id": child_task_id,
                    "label": "graphiti add_episode",
                    "scheduler_task": task,
                    "state": "waking",
                    "started_at": datetime.now(timezone.utc).isoformat(),
                    "details": build_episode_child_details(
                        attempt=attempt,
                        step_key="add_episode",
                        step_label="Graphiti add_episode",
                        source=source_description,
                    ),
                },
            )
        wake_started = perf_counter()
        logger.debug("Graphiti add_episode wake begin name=%s source=%s", name, source_description)
        record_lifecycle_event(
            component="graphiti_client",
            event="add_episode_wake",
            state="started",
            episode_uuid=episode_uuid,
            details={"name": name, "source": source_description, "task": task, "child_task_id": child_task_id},
        )
        await self._ensure_graphiti_endpoints_alive(task=task)
        logger.debug(
            "Graphiti add_episode wake complete name=%s duration_ms=%s llm=%s embed=%s reranker=%s",
            name,
            int((perf_counter() - wake_started) * 1000),
            self.llm_base_url,
            self.embed_base_url,
            self.reranker_base_url,
        )
        record_lifecycle_event(
            component="graphiti_client",
            event="add_episode_wake",
            state="completed",
            episode_uuid=episode_uuid,
            details={
                "name": name,
                "llm": self.llm_base_url,
                "embed": self.embed_base_url,
                "reranker": self.reranker_base_url,
                "task": task,
                "child_task_id": child_task_id,
            },
        )
        if episode_uuid and uses_scheduler_trace:
            await emit_scheduler_task_event(
                parent_job_id=episode_uuid,
                parent_label=name,
                parent_state="graphiti_extracting",
                child={
                    "id": child_task_id,
                    "label": "graphiti add_episode",
                    "scheduler_task": task,
                    "state": "requesting",
                    "started_at": datetime.now(timezone.utc).isoformat(),
                    "details": build_episode_child_details(
                        attempt=attempt,
                        step_key="add_episode",
                        step_label="Graphiti add_episode",
                        source=source_description,
                    ),
                },
            )

        request_started = perf_counter()
        logger.debug("Graphiti add_episode request begin name=%s", name)
        record_lifecycle_event(
            component="graphiti_client",
            event="add_episode_request",
            state="started",
            episode_uuid=episode_uuid,
            details={"name": name, "task": task, "child_task_id": child_task_id},
        )
        # graphiti_core.Graphiti.add_episode() does not currently accept a caller-supplied
        # external episode ID. We use episode_uuid for tracing/scheduler correlation here,
        # then reconcile by anchor name if a local timeout may have hidden a remote success.
        result = await self._llm_breaker.call(
            lambda: self._await_add_episode_request(
                awaitable=self.client.add_episode(
                    name=name,
                    episode_body=episode_body,
                    source_description=source_description,
                    reference_time=reference_time,
                    group_id=group_id,
                ),
                task=task,
                episode_uuid=episode_uuid,
                child_task_id=child_task_id,
            )
        )
        logger.debug(
            "Graphiti add_episode request complete name=%s duration_ms=%s",
            name,
            int((perf_counter() - request_started) * 1000),
        )
        record_lifecycle_event(
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
        if episode_uuid and uses_scheduler_trace:
            await emit_scheduler_task_event(
                parent_job_id=episode_uuid,
                parent_label=name,
                parent_state="stamping",
                child={
                    "id": child_task_id,
                    "label": "graphiti add_episode",
                    "scheduler_task": task,
                    "state": "completed",
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                    "outcome": "completed",
                    "details": build_episode_child_details(
                        attempt=attempt,
                        step_key="add_episode",
                        step_label="Graphiti add_episode",
                        source=source_description,
                    ),
                },
            )
        return result

    async def search(self, query: str, **kwargs: Any) -> Any:
        """Delegate graph search to Graphiti."""
        await self._ensure_graphiti_endpoints_alive(task="memory: graphiti search")

        return await self.client.search(query, **kwargs)

    async def embed_query(self, query: str) -> list[float]:
        """Embed a retrieval query with Graphiti's already-resolved embedder."""
        if self.embedder_ref is None:
            raise RuntimeError("Graphiti embedder is unavailable")
        await self._ensure_graphiti_endpoints_alive(task="memory: content-vector query embedding")
        vector = await self._embed_breaker.call(
            lambda: self.embedder_ref.create(query)
        )
        return [float(value) for value in vector]

    async def search_scored(
        self,
        query: str,
        *,
        num_results: int = 50,
        group_ids: list[str] | None = None,
    ) -> list[tuple[str, str, float]]:
        """Vector + fulltext search returning entity nodes with similarity scores.

        Returns a list of (node_uuid, node_name, similarity_score) tuples.
        Episode nodes are excluded: Graphiti's SearchResults.nodes is typed
        list[EntityNode] (separate from .episodes), and we additionally skip
        any node whose labels include 'Episodic' as a defensive check.
        """
        await self._ensure_graphiti_endpoints_alive(task="memory: graphiti search_scored")
        from graphiti_core.search.search_config import (
            NodeSearchConfig,
            NodeSearchMethod,
            NodeReranker,
            SearchConfig,
        )

        config = SearchConfig(
            node_config=NodeSearchConfig(
                search_methods=[
                    NodeSearchMethod.bm25,
                    NodeSearchMethod.cosine_similarity,
                ],
                reranker=NodeReranker.rrf,
            ),
            limit=num_results,
        )
        try:
            results = await self.client.search_(
                query,
                config,
                group_ids=group_ids,
            )
        except Exception as exc:
            if _is_vector_dimension_mismatch_error(exc):
                logger.warning(
                    "Graphiti search_scored falling back to bm25-only after vector dimension mismatch: %s",
                    exc,
                )
                fallback_config = SearchConfig(
                    node_config=NodeSearchConfig(
                        search_methods=[NodeSearchMethod.bm25],
                        reranker=NodeReranker.rrf,
                    ),
                    limit=num_results,
                )
                results = await self.client.search_(
                    query,
                    fallback_config,
                    group_ids=group_ids,
                )
            else:
                logger.error(
                    "Graphiti fused node search failed query=%r; retrying isolated BM25/cosine lanes: %s: %s",
                    query,
                    exc.__class__.__name__,
                    exc,
                    exc_info=True,
                )
                ranked = await self.search_ranked_by_method(
                    query,
                    methods=["bm25", "cosine_similarity"],
                    num_results=num_results,
                    group_ids=group_ids,
                )
                from graphiti_core.search.search_utils import rrf

                lane_order = ["bm25", "cosine_similarity"]
                names = {
                    uuid: name
                    for method in lane_order
                    for uuid, name in ranked.get(method, [])
                }
                uuids, scores = rrf([
                    [uuid for uuid, _name in ranked.get(method, [])]
                    for method in lane_order
                    if ranked.get(method)
                ])
                return [
                    (uuid, names.get(uuid, uuid), float(score))
                    for uuid, score in zip(uuids[:num_results], scores[:num_results])
                ]

        scored: list[tuple[str, str, float]] = []
        for node, score in zip(results.nodes, results.node_reranker_scores):
            try:
                if "Episodic" in getattr(node, "labels", []):
                    continue
                uuid = str(node.uuid or "").strip()
                if not uuid:
                    raise ValueError("candidate node has no uuid")
                scored.append((uuid, str(node.name or uuid), float(score)))
            except Exception as exc:
                logger.error(
                    "Graphiti search_scored skipped malformed result uuid=%r name=%r: %s: %s",
                    getattr(node, "uuid", None),
                    getattr(node, "name", None),
                    exc.__class__.__name__,
                    exc,
                    exc_info=True,
                )

        return scored

    async def count_similar_by_cosine(
        self,
        query: str,
        *,
        exclude_uuid: str,
        min_cosine: float,
        limit: int = 50,
        group_ids: list[str] | None = None,
    ) -> int:
        """Count DISTINCT entity nodes whose true cosine similarity to `query` is > min_cosine.

        Cosine-only search with sim_min_score as a genuine cosine floor (applied in the vector
        search, strict `>`, before reranking). Returns the count of qualifying neighbors — distinct
        by uuid, excluding `exclude_uuid` and Episodic nodes. Returns -1 if the similarity search is
        unavailable (advisory, not critical — mirrors the -1 contract _count_similar_nodes has today).

        The cap (limit+1 requested, up to limit counted) is intentional: compute_sharpness =
        1/(1+count) saturates below 0.1 by ~9 neighbors and below 0.2 by ~4, so any cap >= ~10
        cannot change a decision — the cap is immaterial to the gates.
        """
        from graphiti_core.search.search_config import (
            NodeSearchConfig,
            NodeSearchMethod,
            NodeReranker,
            SearchConfig,
        )

        config = SearchConfig(
            node_config=NodeSearchConfig(
                search_methods=[NodeSearchMethod.cosine_similarity],
                reranker=NodeReranker.rrf,
                sim_min_score=min_cosine,
            ),
            limit=limit + 1,
        )
        try:
            await self._ensure_graphiti_endpoints_alive(task="memory: graphiti count_similar_by_cosine")
            results = await self.client.search_(
                query,
                config,
                group_ids=group_ids,
            )
        except Exception as exc:
            # Any exception (including vector-dimension-mismatch) returns -1.
            # A dimension mismatch means the cosine index is unusable and there is no
            # lawful similarity signal. No fallback to BM25 — lexical count is not a
            # uniqueness measure and would reintroduce the scale mismatch.
            logger.warning(
                "Graphiti count_similar_by_cosine failed (cosine index unavailable): %s",
                exc,
            )
            return -1

        # Collect distinct uuids, excluding self and Episodic nodes
        seen_uuids: set[str] = set()
        for node in results.nodes:
            if "Episodic" in getattr(node, "labels", []):
                continue
            if node.uuid == exclude_uuid:
                continue
            seen_uuids.add(node.uuid)

        return len(seen_uuids)

    async def search_edges_scored(
        self,
        query: str,
        *,
        num_results: int = 20,
        group_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Hybrid search over RELATES_TO fact EDGES, returning dated facts with scores.

        The entity-node search paths (:meth:`search_scored`,
        :meth:`search_ranked_by_method`) return entity *names*; this returns the
        *facts* that connect entities — ``EntityEdge.fact`` strings like
        "the dealership replaced the GPS system on 2023-03-22". For "what
        happened" queries the answer lives on the edge, not the node, so this is
        the candidate source node search structurally cannot surface.

        Returns a list of dicts in best-first order, each with keys ``uuid``,
        ``fact``, ``score`` (edge RRF reranker score) and the edge's bitemporal
        anchors ``created_at`` / ``valid_at`` / ``invalid_at`` / ``expired_at``
        (ISO-8601 strings or ``None``). Those anchors are the edge's advantage
        over entity nodes — the TemporalOracle scores on them — so the recall
        path threads them into the oracle metadata. Edges with an empty ``fact``
        are skipped. Mirrors :meth:`search_scored`'s BM25-only fallback on a
        vector-dimension mismatch.
        """
        await self._ensure_graphiti_endpoints_alive(task="memory: graphiti search_edges_scored")
        from graphiti_core.search.search_config import (
            EdgeSearchConfig,
            EdgeSearchMethod,
            EdgeReranker,
            SearchConfig,
        )

        config = SearchConfig(
            edge_config=EdgeSearchConfig(
                search_methods=[
                    EdgeSearchMethod.bm25,
                    EdgeSearchMethod.cosine_similarity,
                ],
                reranker=EdgeReranker.rrf,
            ),
            limit=num_results,
        )
        try:
            results = await self.client.search_(query, config, group_ids=group_ids)
        except Exception as exc:
            if not _is_vector_dimension_mismatch_error(exc):
                raise
            logger.warning(
                "Graphiti search_edges_scored falling back to bm25-only after vector dimension mismatch: %s",
                exc,
            )
            fallback_config = SearchConfig(
                edge_config=EdgeSearchConfig(
                    search_methods=[EdgeSearchMethod.bm25],
                    reranker=EdgeReranker.rrf,
                ),
                limit=num_results,
            )
            results = await self.client.search_(query, fallback_config, group_ids=group_ids)

        def _iso(dt: Any) -> str | None:
            return dt.isoformat() if dt is not None else None

        scored: list[dict[str, Any]] = []
        for edge, score in zip(results.edges, results.edge_reranker_scores):
            fact = (getattr(edge, "fact", None) or "").strip()
            if not fact:
                continue
            scored.append(
                {
                    "uuid": edge.uuid,
                    "fact": fact,
                    "score": score,
                    "created_at": _iso(getattr(edge, "created_at", None)),
                    "valid_at": _iso(getattr(edge, "valid_at", None)),
                    "invalid_at": _iso(getattr(edge, "invalid_at", None)),
                    "expired_at": _iso(getattr(edge, "expired_at", None)),
                    # Endpoint entity nodes: the RICH candidates the edge points at (a
                    # dated fact edge connects e.g. "dealership" -> "GPS system", whose
                    # node summaries carry the answer with surrounding context). Used by
                    # the "pointer" fact-edge mode to hydrate node context instead of
                    # injecting the terse fact as a standalone (context-shredding) answer.
                    "source_node_uuid": getattr(edge, "source_node_uuid", None),
                    "target_node_uuid": getattr(edge, "target_node_uuid", None),
                }
            )

        return scored

    async def search_ranked_by_method(
        self,
        query: str,
        *,
        methods: list[str],
        num_results: int = 50,
        group_ids: list[str] | None = None,
    ) -> dict[str, list[tuple[str, str]]]:
        """Run each search method as its own pass; return rank-ordered hits per method.

        Unlike :meth:`search_scored` -- which fuses BM25 + cosine into one opaque
        RRF score and discards which method found what -- this keeps each method
        separate so the caller can attribute a candidate's source and blend on
        rank (see ``services/hybrid_retrieval.py``).

        Returns a dict keyed by the method name (``"cosine_similarity"`` /
        ``"bm25"``); each value is a list of ``(node_uuid, node_name)`` tuples in
        best-first rank order. Episode nodes are excluded, as in
        :meth:`search_scored`. If the cosine pass hits a vector-dimension
        mismatch its result is an empty list (BM25 is unaffected); any other
        method's mismatch is not silently swallowed.
        """
        await self._ensure_graphiti_endpoints_alive(task="memory: graphiti search_ranked_by_method")
        from graphiti_core.search.search_filters import SearchFilters
        from graphiti_core.search.search_utils import (
            node_fulltext_search,
            node_similarity_search,
        )

        supported = {"bm25", "cosine_similarity"}
        effective_group_ids = group_ids if group_ids and group_ids != [""] else None
        search_filter = SearchFilters()
        query_vector: list[float] | None = None

        ranked: dict[str, list[tuple[str, str]]] = {}
        failures: dict[str, Exception] = {}
        for method in methods:
            if method not in supported:
                raise ValueError(f"Unsupported search method: {method!r}")
            try:
                if method == "bm25":
                    nodes = await node_fulltext_search(
                        self.client.driver,
                        query,
                        search_filter,
                        effective_group_ids,
                        2 * num_results,
                    )
                else:
                    if query_vector is None:
                        query_vector = await self.embed_query(query)
                    nodes = await node_similarity_search(
                        self.client.driver,
                        query_vector,
                        search_filter,
                        effective_group_ids,
                        2 * num_results,
                    )
            except Exception as exc:
                if method == "cosine_similarity" and _is_vector_dimension_mismatch_error(exc):
                    logger.warning(
                        "Graphiti cosine pass skipped after vector dimension mismatch: %s", exc
                    )
                    ranked[method] = []
                    failures[method] = exc
                    continue
                logger.error(
                    "Graphiti %s lane failed query=%r; other retrieval lanes will continue: %s: %s",
                    method,
                    query,
                    exc.__class__.__name__,
                    exc,
                    exc_info=True,
                )
                ranked[method] = []
                failures[method] = exc
                continue

            hits: list[tuple[str, str]] = []
            for node in nodes:
                try:
                    if "Episodic" in getattr(node, "labels", []):
                        continue
                    uuid = str(node.uuid or "").strip()
                    if not uuid:
                        raise ValueError("candidate node has no uuid")
                    hits.append((uuid, str(node.name or uuid)))
                except Exception as exc:
                    logger.error(
                        "Graphiti %s lane skipped malformed result uuid=%r name=%r: %s: %s",
                        method,
                        getattr(node, "uuid", None),
                        getattr(node, "name", None),
                        exc.__class__.__name__,
                        exc,
                        exc_info=True,
                    )
            ranked[method] = hits

        if methods and len(failures) == len(methods):
            detail = ", ".join(
                f"{method}={exc.__class__.__name__}" for method, exc in failures.items()
            )
            raise RuntimeError(f"all requested Graphiti search lanes failed ({detail})")

        return ranked

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
