"""Construction-side implementation for :mod:`menhir.infrastructure.graphiti_client`.

``GraphitiClient.from_settings_with_capabilities`` delegates here; the body is the pre-split
implementation moved verbatim. Every module-level name the body needs (the graphiti_core
import-guard bindings, the patch entry points, provider plumbing, the logger) is resolved
through the original ``graphiti_client`` module at call time — the same lookups the pre-split
code performed — so monkeypatching those names on ``graphiti_client`` keeps working unchanged.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from menhir.config import MemorySettings

if TYPE_CHECKING:  # pragma: no cover - typing only
    from menhir.infrastructure.graphiti_client import GraphitiClient


def from_settings_with_capabilities(
    cls: type[GraphitiClient],
    settings: MemorySettings,
    *,
    llm_enabled: bool = True,
    reranker_enabled: bool = True,
) -> GraphitiClient:
    """Build a Graphiti client from runtime settings with optional degraded collaborators."""

    from menhir.infrastructure import graphiti_client as base

    if base._GRAPHITI_IMPORT_ERROR is not None:
        raise ModuleNotFoundError(
            "graphiti_core is required to construct a GraphitiClient from settings."
        ) from base._GRAPHITI_IMPORT_ERROR

    base._apply_graphiti_construction_patches(settings)
    llm_provider = base.ProviderConfig.for_graphiti_llm(settings)
    embed_provider = base.ProviderConfig.for_graphiti_embedder(settings)
    reranker_provider = base.ProviderConfig.for_graphiti_reranker(settings)
    # Make the effective provider resolution visible at startup. Provider config is
    # read straight from the environment, so an inherited/ambient var can silently
    # override the intended config; log the resolved chain (never the api keys) so a
    # misconfiguration is obvious from the logs instead of requiring a probe.
    base.logger.info(
        "Graphiti providers resolved: llm=%s base=%s model=%s | embed=%s base=%s model=%s | reranker=%s base=%s",
        llm_provider.kind, llm_provider.base_url, llm_provider.chat_model,
        embed_provider.kind, embed_provider.base_url, embed_provider.embed_model,
        reranker_provider.kind, reranker_provider.base_url,
    )
    if not llm_provider.supports_graphiti_openai_contract():
        raise NotImplementedError(
            "Graphiti provider must currently be openai_compat or openai. "
            "Other providers require a dedicated Graphiti bridge."
        )
    if not embed_provider.supports_graphiti_openai_contract():
        raise NotImplementedError(
            "Graphiti embed provider must currently be openai_compat or openai."
        )
    if not reranker_provider.supports_graphiti_openai_contract():
        raise NotImplementedError(
            "Graphiti reranker provider must currently be openai_compat or openai."
        )

    llama_base_url = llm_provider.base_url
    _embed_cache = base.get_embedding_cache()
    async_client = base.build_async_openai_client(
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
        llm_client = base.OpenAIGenericClient(
            config=base.LLMConfig(
                api_key=llm_provider.api_key,
                base_url=llama_base_url,
                model=llm_provider.chat_model,
                temperature=0,
            ),
            client=async_client,
            max_tokens=settings.llm_max_tokens,
        )
    embed_base_url = embed_provider.base_url
    embed_dimension = base.expected_graphiti_embedding_dimension(settings)
    embed_client = (
        async_client
        if embed_base_url == llama_base_url
        else base.build_async_openai_client(
            base_url=embed_base_url,
            api_key=embed_provider.api_key,
            settings=settings,
            embedding_cache=_embed_cache,
        )
    )
    embedder = base.OpenAIEmbedder(
        config=base.OpenAIEmbedderConfig(
            api_key=embed_provider.api_key,
            base_url=embed_base_url,
            embedding_model=embed_provider.embed_model,
            **({"embedding_dim": embed_dimension} if embed_dimension is not None else {}),
        ),
        client=embed_client,
    )
    reranker_base_url = reranker_provider.base_url
    reranker_client = (
        async_client
        if reranker_base_url == llama_base_url
        else base.build_async_openai_client(
            base_url=reranker_base_url,
            api_key=reranker_provider.api_key,
            settings=settings,
        )
    )
    cross_encoder = None
    if reranker_enabled:
        cross_encoder = base.OpenAIRerankerClient(
            config=base.LLMConfig(
                api_key=reranker_provider.api_key,
                base_url=reranker_base_url,
                model=reranker_provider.chat_model,
            ),
            client=reranker_client,
        )
    graph_driver = base.Neo4jDriver(
        uri=settings.neo4j_uri,
        user=settings.neo4j_user,
        password=settings.neo4j_password,
        database=settings.neo4j_database,
    )
    return cls(
        client=base.Graphiti(
            uri=settings.neo4j_uri,
            user=settings.neo4j_user,
            password=settings.neo4j_password,
            graph_driver=graph_driver,
            llm_client=llm_client,
            embedder=embedder,
            cross_encoder=cross_encoder,
        ),
        llm_base_url=llama_base_url,
        embed_base_url=embed_base_url,
        reranker_base_url=reranker_base_url,
        llm_provider_kind=llm_provider.kind.value,
        embed_provider_kind=embed_provider.kind.value,
        reranker_provider_kind=reranker_provider.kind.value,
        llm_client_ref=llm_client,
        embedder_ref=embedder,
        reranker_ref=cross_encoder,
        embedding_cache=_embed_cache,
    )
