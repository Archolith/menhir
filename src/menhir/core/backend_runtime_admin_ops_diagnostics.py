"""Diagnostics and provider-configuration operations for the in-process backend adapter."""

from __future__ import annotations

from typing import Any

from menhir.config import redact_uri_credentials
from menhir.infrastructure.providers import ProviderConfig

from .backend_shared import _to_jsonable


class RuntimeDiagnosticsOpsMixin:
    """Diagnostics and provider-configuration operations for the in-process backend adapter."""

    async def fetch_memory_overview(self, namespace: str | None = None) -> dict[str, Any]:
        # Threaded, not dropped: CF-239 was this exact method shape forgetting its namespace on
        # the runtime limb while the HTTP limb forwarded it.
        return _to_jsonable(
            await self._off_loop(
                self.built.graph_adapter.fetch_memory_overview, namespace
            )
        )

    async def circuit_breaker_snapshots(self) -> dict[str, dict[str, Any]]:
        return _to_jsonable(
            await self._off_loop(self.built.graphiti_client.circuit_breaker_snapshots)
        )

    async def embedding_cache_stats(self) -> dict[str, int]:
        return _to_jsonable(
            await self._off_loop(self.built.graphiti_client.embedding_cache_stats)
        )

    async def get_provider_config(self) -> dict[str, Any]:
        settings = self.built.settings
        chat_provider = ProviderConfig.for_chat(settings)
        graphiti_llm_provider = ProviderConfig.for_graphiti_llm(settings)
        graphiti_embed_provider = ProviderConfig.for_graphiti_embedder(settings)
        graphiti_reranker_provider = ProviderConfig.for_graphiti_reranker(settings)
        # Every URL in this dict is disclosed to callers through SystemMetadataResource, so
        # userinfo is stripped HERE rather than at each consumer -- one choke point, and a new
        # consumer cannot reintroduce the leak by forgetting to redact.
        return {
            "neo4j_uri": redact_uri_credentials(getattr(settings, "neo4j_uri", "")),
            "chat_provider": chat_provider.kind.value,
            "graphiti_provider": graphiti_llm_provider.kind.value,
            "graphiti_embed_provider": graphiti_embed_provider.kind.value,
            "graphiti_reranker_provider": graphiti_reranker_provider.kind.value,
            "neo4j_database": getattr(settings, "neo4j_database", ""),
            "local_llm_base_url": redact_uri_credentials(
                getattr(settings, "local_llm_base_url", "")
            ),
            "local_llm_embed_base_url": redact_uri_credentials(
                getattr(settings, "local_llm_embed_base_url", "")
            ),
            "chat_model": chat_provider.chat_model,
            "graphiti_llm_chat_model": graphiti_llm_provider.chat_model,
            "embed_model": chat_provider.embed_model,
            "graphiti_embed_model": graphiti_embed_provider.embed_model,
            "backend_url": redact_uri_credentials(getattr(settings, "backend_url", "")),
        }
